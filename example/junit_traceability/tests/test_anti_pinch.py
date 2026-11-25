"""车窗控制测试用例。

用例与需求的关联通过 StrictDoc 的 @relation 标记声明。
"""


def test_reverse_within_100ms():
    """
    @relation(SWR-0001, scope=function)
    """
    assert 80 <= 100


def test_position_accuracy():
    """
    @relation(SWR-0002, scope=function)
    """
    assert 0.8 <= 1.0
