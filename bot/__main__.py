from .cli import main

# 中文说明：
# 这是 `python -m bot ...` 真正的程序入口。
# Python 执行一个“包”时（例如 `python -m bot`），会先找到 bot/__main__.py，
# 再运行这里面的代码。真正的命令行解析都在 bot/cli.py 的 main() 里。
#
# 为什么用 `raise SystemExit(main())`，而不是直接 `main()`？
#   main() 返回的是“退出码”（0=成功，非 0=失败）。SystemExit 会把这个数字
#   交给操作系统，PowerShell、批处理脚本和计划任务就能据此判断运行结果。
#   如果直接调用 main()，退出码永远是 0，自动化脚本会误以为一切正常。
if __name__ == "__main__":
    raise SystemExit(main())
