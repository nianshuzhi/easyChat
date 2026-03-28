import sys
import time
import datetime
import threading
import keyboard

from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *


# 定时发送子线程类
class ClockThread(QThread):
    # 定义信号：用于通知GUI显示错误信息
    error_signal = pyqtSignal(str)
    # 定义信号：用于通知GUI线程执行发送（避免在子线程里直接操作Qt控件）
    send_signal = pyqtSignal(int, int, str)

    def __init__(self):
        super().__init__()
        # 是否正在定时
        self.time_counting = False
        # 发送信息的函数
        self.send_func = None
        # 定时列表
        self.clocks = None
        # 是否防止自动下线
        self.prevent_offline = False
        self.prevent_func = None
        # 每隔多少分钟进行一次防止自动下线操作
        self.prevent_count = 60

        # 新增：用于存储已执行过的任务标识，防止重复执行
        self.executed_tasks = set()

        # 用于防止掉线的内部计时器
        self._prevent_timer = 0

        # 由GUI线程维护并推送进来的“定时列表快照”（避免子线程直接访问Qt控件）
        self._schedules_lock = threading.Lock()
        self._schedules = []

        # 用于唤醒等待（定时列表变更/停止定时/防掉线更早触发）
        self._wakeup_event = threading.Event()

        # 允许“迟到执行”的宽限时间（秒）：避免因为线程调度/系统短暂卡顿错过窗口
        self.late_grace_seconds = 10 * 60

    def __del__(self):
        self.wait()

    def reset_state(self):
        """开始定时前重置状态，避免重复/过期标记影响下一次开始。"""
        self.executed_tasks.clear()
        self._prevent_timer = self.prevent_count * 60
        self._wakeup_event.set()

    def stop(self):
        """停止定时，并立即唤醒线程以便快速退出。"""
        self.time_counting = False
        self._wakeup_event.set()

    def set_schedules(self, schedules):
        """由GUI线程调用：推送最新的定时列表（字符串列表）。"""
        with self._schedules_lock:
            self._schedules = list(schedules or [])
        self._wakeup_event.set()

    def _get_schedules_snapshot(self):
        with self._schedules_lock:
            return list(self._schedules)

    def run(self):
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            # 初始化防止掉线的计时器，设置为 prevent_count 分钟对应的秒数
            self._prevent_timer = self.prevent_count * 60

            while self.time_counting:
                schedules = self._get_schedules_snapshot()
                now = datetime.datetime.now()

                # --- 1) 解析任务、执行到期任务、寻找下一次触发 ---
                next_event_time = None

                for task_id in schedules:
                    if not task_id or task_id in self.executed_tasks:
                        continue

                    try:
                        parts = task_id.split(" ")
                        if len(parts) < 6:
                            raise ValueError("任务格式错误，应为 'Y m d H M st-ed'")

                        clock_str = " ".join(parts[:5])
                        dt_obj = datetime.datetime.strptime(clock_str, "%Y %m %d %H %M")

                        st_ed = parts[5]
                        st, ed = st_ed.split('-', 1)
                        st_i, ed_i = int(st), int(ed)

                        if dt_obj <= now:
                            late_seconds = (now - dt_obj).total_seconds()
                            if 0 <= late_seconds <= self.late_grace_seconds:
                                # 通过信号让GUI线程去执行发送，避免子线程直接访问Qt控件
                                self.send_signal.emit(st_i, ed_i, task_id)
                            # 无论是否执行，都标记为已处理，防止重复触发
                            self.executed_tasks.add(task_id)
                            continue

                        # 未来任务：找最近的一次
                        if next_event_time is None or dt_obj < next_event_time:
                            next_event_time = dt_obj

                    except Exception as e:
                        # 单条任务解析失败：跳过并提示，但不要直接终止整个定时线程
                        error_msg = f"定时任务格式解析失败，将跳过：{task_id}\n错误信息：{e}"
                        print(error_msg)
                        self.error_signal.emit(error_msg)
                        self.executed_tasks.add(task_id)

                # --- 2) 计算等待时间（支持列表变更/停止时立刻唤醒） ---
                wait_seconds = None
                if next_event_time is not None:
                    wait_seconds = max(0.0, (next_event_time - now).total_seconds())

                # 防止掉线：取更早发生的那个
                if self.prevent_offline:
                    if wait_seconds is None:
                        wait_seconds = float(self._prevent_timer)
                    else:
                        wait_seconds = min(float(wait_seconds), float(self._prevent_timer))

                # 没有未来任务也没开启防掉线：避免忙等，稍微休眠并等待列表变更
                if wait_seconds is None:
                    wait_seconds = 1.0

                start_mono = time.monotonic()
                self._wakeup_event.wait(timeout=wait_seconds)
                self._wakeup_event.clear()
                elapsed = time.monotonic() - start_mono

                # 如果此时已经停止定时，直接退出，避免停止后仍执行防掉线等动作
                if not self.time_counting:
                    break

                # --- 3) 更新防掉线计时器并执行防掉线 ---
                if self.prevent_offline:
                    self._prevent_timer -= elapsed
                    if self._prevent_timer <= 0:
                        self._prevent_timer = 0
                        if self.prevent_func:
                            try:
                                self.prevent_func()
                            finally:
                                # 重置计时器
                                self._prevent_timer = self.prevent_count * 60


class MyListWidget(QListWidget):
    """支持双击可编辑的QListWidget"""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)  # 设置选择多个

        # 双击可编辑
        self.edited_item = self.currentItem()
        self.close_flag = True
        self.doubleClicked.connect(self.item_double_clicked)
        self.currentItemChanged.connect(self.close_edit)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        """回车事件，关闭edit"""
        super().keyPressEvent(e)
        if e.key() == Qt.Key_Return:
            if self.close_flag:
                self.close_edit()
            self.close_flag = True

    def edit_new_item(self) -> None:
        """edit一个新的item"""
        self.close_flag = False
        self.close_edit()
        count = self.count()
        self.addItem('')
        item = self.item(count)
        self.edited_item = item
        self.openPersistentEditor(item)
        self.editItem(item)

    def item_double_clicked(self, modelindex: QModelIndex) -> None:
        """双击事件"""
        self.close_edit()
        item = self.item(modelindex.row())
        self.edited_item = item
        self.openPersistentEditor(item)
        self.editItem(item)

    def close_edit(self, *_) -> None:
        """关闭edit"""
        if self.edited_item and self.isPersistentEditorOpen(self.edited_item):
            self.closePersistentEditor(self.edited_item)


class MultiInputDialog(QDialog):
    """
    用于用户输入的输入框，可以根据传入的参数自动创建输入框
    """
    def __init__(self, inputs: list, default_values: list = None, parent=None) -> None:
        """
        inputs: list, 代表需要input的标签，如['姓名', '年龄']
        default_values: list, 代表默认值，如['张三', '18']
        """
        super().__init__(parent)
        
        layout = QVBoxLayout(self)
        self.inputs = []
        for n, i in enumerate(inputs):
            layout.addWidget(QLabel(i))
            input = QLineEdit(self)

            # 设置默认值
            if default_values is not None:
                input.setText(default_values[n])

            layout.addWidget(input)
            self.inputs.append(input)
            
        ok_button = QPushButton("确认")
        ok_button.clicked.connect(self.accept)
        
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        
        button_layout = QHBoxLayout()
        button_layout.addWidget(ok_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def get_input(self):
        """获取用户输入"""
        return [i.text() for i in self.inputs]


class FileDialog(QDialog):
    """
    文件选择框
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.inputs = []
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("请指定发送给哪些用户(1,2,3代表发送给前三位用户)，如需全部发送请忽略此项"))
        input = QLineEdit(self)
        layout.addWidget(input)
        self.inputs.append(input)
        
        # 选择文件
        choose_layout = QHBoxLayout()

        path = QLineEdit(self)
        choose_layout.addWidget(path)
        self.inputs.append(path)

        file_button = QPushButton("选择文件")
        file_button.clicked.connect(self.select)
        choose_layout.addWidget(file_button)

        layout.addLayout(choose_layout)
        
        # 确认按钮
        ok_button = QPushButton("确认")
        ok_button.clicked.connect(self.accept)

        # 取消按钮
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)

        # 按钮布局
        button_layout = QHBoxLayout()
        button_layout.addWidget(ok_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def select(self):
        path_input = self.inputs[1]
        # 修改为支持多文件选择
        paths = QFileDialog.getOpenFileNames(self, '打开文件', '/home')[0]
        if paths:
            # 将多个文件路径用分号连接显示
            path_input.setText(";".join(paths))
    
    def get_input(self):
        """获取用户输入"""
        return [i.text() for i in self.inputs]


class MyDoubleSpinBox(QWidget):
    def __init__(self, desc: str, **kwargs):
        """
        附带标签的DoubleSpinBox，支持小数输入
        Args:
            desc: 默认的标签
        """
        super().__init__(**kwargs)

        layout = QHBoxLayout()

        self.desc = desc
        self.label = QLabel(desc)

        self.spin_box = QDoubleSpinBox()
        self.spin_box.setDecimals(1)
        self.spin_box.setSingleStep(0.1)
        self.spin_box.setRange(0.0, 60.0)

        layout.addWidget(self.label)
        layout.addWidget(self.spin_box)
        self.setLayout(layout)


class MySpinBox(QWidget):
    def __init__(self, desc: str, **kwargs):
        """
        附带标签的SpinBox
        Args:
            desc: 默认的标签
        """
        super().__init__(**kwargs)

        layout = QHBoxLayout()

        # 初始化标签
        self.desc = desc
        self.label = QLabel(desc)
        # self.label.setAlignment(Qt.AlignCenter)

        # 初始化计数器
        self.spin_box = QSpinBox()
        # self.spin_box.valueChanged.connect(self.valuechange)

        layout.addWidget(self.label)
        layout.addWidget(self.spin_box)
        self.setLayout(layout)

    # def valuechange(self):
    #     self.label.setText(f"{self.desc}: {self.spin_box.value()}")