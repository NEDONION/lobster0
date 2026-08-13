"""对着**真实** lark_channel SDK 校验我们实际传出去的构造参数。

## 为什么必须有这个文件

其余飞书测试都跑在 ``tests/fakes/fake_channel.py`` 的假 SDK 上，而那个假 SDK 的
每个工厂都是 ``**values`` ——来者不拒。于是 2026-08-13 出现过这样一次事故：
``_build_channel`` 给 ``FeishuChannel(...)`` 传了 ``media_cache=``，假 SDK 照单全收，
全部单测通过并合入；真实 SDK 根本没有这个构造关键字，Gateway 一起来就死在

    TypeError: FeishuChannel.__init__() got an unexpected keyword argument 'media_cache'

而 CLI 把异常收敛成了一句 "gateway startup or runtime failed"，连线索都没有。

假 SDK 换来的是不碰网络的快速测试，这个取舍是对的；代价是它对**签名**零约束。
本文件专门补上那一格：只做静态签名比对，不建连接、不发请求、不需要任何凭据，
因此可以和其余单测一起无条件运行。
"""

import dataclasses
import inspect
import unittest

import lark_channel

from lobster0.channels.feishu import FeishuTransport


class FeishuSdkContractTest(unittest.TestCase):
    """锁定我们与 SDK 之间真正用到的那部分接口。"""

    def test_every_constructor_keyword_we_pass_exists_on_the_real_sdk(self) -> None:
        """``_build_channel`` 用到的每个关键字都必须是真实 SDK 接受的。

        直接从源码里取我们写死的那串关键字，而不是另抄一份常量——抄一份就会
        和实现漂移，漂移之后这个测试就只能证明"两份常量一致"，什么也拦不住。
        """
        source = inspect.getsource(FeishuTransport._build_channel)
        _, _, call = source.partition("self._sdk.FeishuChannel(")
        self.assertTrue(call, "未能在 _build_channel 中定位 FeishuChannel 构造调用")
        passed = {
            line.strip().split("=", 1)[0]
            for line in call.splitlines()
            # 只取该调用最外层的 ``name=`` 实参：缩进恰好 12 空格，
            # 嵌套的 ChannelConfig / MediaCacheConfig 实参缩进更深。
            if line.startswith(" " * 12)
            and not line.startswith(" " * 13)
            and "=" in line
        }
        self.assertIn("config", passed, "media_cache 必须经由 config 传入")
        self.assertNotIn(
            "media_cache",
            passed,
            "media_cache 不是 FeishuChannel 的构造关键字，只能放进 ChannelConfig",
        )

        accepted = set(
            inspect.signature(lark_channel.FeishuChannel.__init__).parameters
        )
        self.assertEqual(
            passed - accepted,
            set(),
            "这些关键字真实 SDK 不接受，Gateway 启动时会直接 TypeError",
        )

    def test_media_cache_fields_we_set_exist_on_the_real_config(self) -> None:
        """我们设置的媒体缓存字段必须真实存在，否则图片永远不会被缓存到本地。

        字段名写错不会报错——dataclass 会拒绝未知关键字，但我们真正怕的是
        **改名**：SDK 把 ``image_max_bytes`` 换个名字，这里就静默失去限额。
        """
        fields = {field.name for field in dataclasses.fields(lark_channel.MediaCacheConfig)}
        self.assertTrue(
            {"enabled", "root_dir", "ttl_seconds", "image_max_bytes"}.issubset(fields),
            f"MediaCacheConfig 字段已变化：{sorted(fields)}",
        )

    def test_channel_config_carries_media_cache(self) -> None:
        """``media_cache`` 必须仍然挂在 ChannelConfig 上——这是它唯一的入口。"""
        fields = {field.name for field in dataclasses.fields(lark_channel.ChannelConfig)}
        self.assertIn("media_cache", fields)

    def test_explicit_keywords_still_override_the_config_base(self) -> None:
        """SDK 必须保持"``config`` 作底座、显式关键字覆盖"的合并顺序。

        我们依赖这个顺序：ChannelConfig 只为捎带 media_cache，其余配置照旧走
        关键字。如果哪天 SDK 反过来让 config 覆盖关键字，domain / policy /
        inbound 会被 ChannelConfig 的默认值悄悄顶掉——那是静默的错配置，
        比崩溃更难查。
        """
        source = inspect.getsource(lark_channel.FeishuChannel.__init__)
        base, _, rest = source.partition("cfg = config if config is not None")
        self.assertTrue(rest, "SDK 不再以 config 作为合并底座，需要重新确认覆盖顺序")
        self.assertIn("cfg.domain = domain", rest)
        self.assertIn("cfg.inbound = inbound", rest)


if __name__ == "__main__":
    unittest.main()
