import subprocess
import sys


# 用来自动打包成exe程序
def main():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "-F",
        "-w",
        "--noupx",
        "--clean",
        "--name",
        "EasyChat",
        "wechat_gui.py",
    ]

    subprocess.run(cmd, check=True)


if __name__ == '__main__':
    main()
