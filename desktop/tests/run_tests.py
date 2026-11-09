# -*- coding: utf-8 -*-
"""运行全部 unittest 并把原始输出写入 TEST_OUTPUT.txt。

用法：
    项目根\.venv\Scripts\python.exe desktop\tests\run_tests.py
    （纯逻辑测试不依赖第三方库，系统 python 亦可运行）
"""
import datetime
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.dirname(HERE)
sys.path.insert(0, DESKTOP)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    body = stream.getvalue()

    header = (
        "==================== 单元测试原始输出 ====================\n"
        "运行时间: %s\n"
        "Python: %s\n"
        "命令: .venv\\Scripts\\python.exe desktop\\tests\\run_tests.py\n"
        "发现用例文件: test_protocol.py / test_processing.py / test_simulator.py\n"
        "========================================================\n\n"
        % (datetime.datetime.now().isoformat(sep=" "), sys.version.replace("\n", " "))
    )
    summary = (
        "\n==================== 汇总 ====================\n"
        "运行: %d   失败: %d   错误: %d   跳过: %d   成功: %s\n"
        % (result.testsRun, len(result.failures), len(result.errors),
           len(result.skipped), result.wasSuccessful())
    )
    log = header + body + summary
    out_path = os.path.join(HERE, "TEST_OUTPUT.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(log)
    sys.stdout.write(log)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
