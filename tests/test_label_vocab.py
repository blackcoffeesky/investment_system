"""因果层的标签常量必须与状态层词表一致。

因果层不再 import 状态层（解耦），两边各写一份字符串。
这条测试就是防它们漂移的那根绳子。
"""


def test_label_vocab_matches_state():
    from causual.causual_assets import load as load_causal
    from state_layer import state_assets

    for asset in ("ashare", "gold"):
        c = load_causal(asset).C
        st = state_assets.load(asset).config
        assert tuple(c.ORDER) == tuple(st.VALUES), asset
