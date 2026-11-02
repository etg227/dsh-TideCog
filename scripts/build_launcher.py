"""打包 launcher.exe，放在项目根目录（和 config.toml、data/ 在一起）。

先装好桌面版的包（见 requirements-desktop.txt），然后：
    .venv\\Scripts\\python scripts\\build_launcher.py
"""
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent


def main():
    PyInstaller.__main__.run([
        str(ROOT / "launcher.py"),
        "--name", "launcher",
        "--onefile",
        "--noconsole",
        "--icon", str(ROOT / "assets" / "launcher.ico"),
        "--add-data", f"{ROOT / 'xiaojing' / 'console.html'};xiaojing",
        "--add-data", f"{ROOT / 'xiaojing' / 'setup.html'};xiaojing",
        "--add-data", f"{ROOT / 'assets' / 'launcher.ico'};assets",
        # 第一次运行时放到项目文件夹里的默认文件；本机的 config.toml 和 persona.md 不会被打进去
        "--add-data", f"{ROOT / 'config.example.toml'};.",
        "--add-data", f"{ROOT / 'persona.default.md'};.",
        "--add-data", f"{ROOT / 'prompts' / 'system.md'};prompts",
        "--distpath", str(ROOT),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--noconfirm",
        "--clean",
    ])
    print(f"打包好了：{ROOT / 'launcher.exe'}")


if __name__ == "__main__":
    main()
