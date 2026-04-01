from pathlib import Path

from nonebug import App


def test_builtin_stopwords_words():
    """测试默认停用词表内容"""
    builtin_stopwords = set(
        (
            Path(__file__).resolve().parent.parent
            / "nonebot_plugin_wordcloud"
            / "stopword.txt"
        ).read_text("utf8").splitlines()
    )
    assert {
        "占卜",
        "jrlp",
        "还有",
        "信息",
        "以为",
        "其他",
        "这么",
        "的话",
        "反正",
        "不会",
        "要是",
        "天天",
        "只能",
    } <= builtin_stopwords


async def test_stopwords(app: App):
    """测试设置停用词表"""
    from nonebot_plugin_localstore import get_data_file

    from nonebot_plugin_wordcloud.config import plugin_config
    from nonebot_plugin_wordcloud.data_source import analyse_message

    data = get_data_file("nonebot_plugin_wordcloud", "stopwords.txt")
    with data.open("w", encoding="utf8") as f:
        f.write("句子")

    message = "这是一个奇怪的句子。"
    original_stopwords_path = plugin_config.wordcloud_stopwords_path

    try:
        plugin_config.wordcloud_stopwords_path = None
        frequency = analyse_message(message)
        assert "句子" in frequency

        plugin_config.wordcloud_stopwords_path = data
        frequency = analyse_message(message)
        assert "句子" not in frequency
        assert frequency
    finally:
        plugin_config.wordcloud_stopwords_path = original_stopwords_path
