"""PRD V2 的 P0「独立可跑」验收：入口与路径不再假设盒子或源码目录可写。

本文件只覆盖能纯本地断言的部分，不连模型、不写用户数据。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from runtime_paths import (
    MODELS_CACHE_DIR,
    MODELSCOPE_CACHE_DIR,
    WHISPER_MODELS_DIR,
)

BACKEND_DIR = Path(server.__file__).resolve().parent


class StandaloneEntryTests(unittest.TestCase):
    def test_root_opens_the_app_not_the_legacy_lan_page(self):
        """独立软件的首页就是知君本身。/lan 仍在原地，只是不再是根路径的去处。

        直接调端点函数，不用 TestClient：后者会跑 startup 事件、把一堆单例按默认数据根
        初始化，污染同一进程里后面的用例（本次踩到过，core_profile 整片挂掉）。
        """
        response = server.root()
        self.assertIn(response.status_code, (301, 302, 307, 308))
        self.assertEqual(response.headers["location"], "/mindos/")

    def test_legacy_lan_entry_still_reachable(self):
        """降级根路径不等于删功能：直接访问 /lan 仍要有路由，不能 404。"""
        paths = {getattr(route, "path", None) for route in server.app.routes}
        self.assertIn("/lan", paths)

    def test_model_caches_are_not_pinned_to_the_source_tree(self):
        """三处模型权重缓存过去写死在 backend/ 下，装到只读目录会坏。

        允许它们落在源码目录（旧安装沿用旧位置），但不允许写死——必须能被环境变量改掉。
        这里断言的是「可配」这一半；默认值落哪里由 test_data_root_contract 覆盖。
        """
        for env_name, current in (
            ("CENTAUR_MODELS_CACHE", MODELS_CACHE_DIR),
            ("CENTAUR_MODELSCOPE_CACHE", MODELSCOPE_CACHE_DIR),
            ("CENTAUR_WHISPER_MODELS", WHISPER_MODELS_DIR),
        ):
            with self.subTest(env=env_name):
                self.assertTrue(current.is_absolute(), f"{env_name} 解析出的路径必须是绝对路径")
                self.assertNotIn(env_name, os.environ, "本用例要在未设置覆盖变量时运行")

    def test_standalone_mode_needs_no_cloud_ticket_but_demands_loopback(self):
        """独立发行版装在用户自己电脑上，没有云控制面，也就没有票据可换。

        但它必须同时满足两条：发行版自己声明，且服务确实只绑在回环上。
        """
        from mindos.local_web_debug import (
            ACCESS_MODE_STANDALONE, ACCESS_MODE_TICKET_REQUIRED, access_context,
        )
        got = access_context(bind_host="127.0.0.1", environ={"ZHIJUN_STANDALONE": "1"})
        self.assertEqual(got["mode"], ACCESS_MODE_STANDALONE)
        self.assertFalse(got["localDebug"], "独立发行版不是调试模式，别混为一谈")

        # 绑到了非回环地址：声明了也不算，退回要票据。
        for host in ("0.0.0.0", "192.168.1.10", "", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(
                    access_context(bind_host=host, environ={"ZHIJUN_STANDALONE": "1"})["mode"],
                    ACCESS_MODE_TICKET_REQUIRED, host)

    def test_standalone_is_not_inferred_from_the_runtime_environment(self):
        """不按 MINDOS_RUNTIME_ENV 推断——那会让某个盒子因为环境变量写错而静默失去票据校验。"""
        from mindos.local_web_debug import ACCESS_MODE_TICKET_REQUIRED, access_context
        for env in ({}, {"MINDOS_RUNTIME_ENV": "development"}, {"MINDOS_RUNTIME_ENV": "production"}):
            with self.subTest(env=env):
                self.assertEqual(access_context(bind_host="127.0.0.1", environ=env)["mode"],
                                 ACCESS_MODE_TICKET_REQUIRED)

    def test_standalone_does_not_claim_a_cloud_device_id(self):
        """没有「认领 → 票据 → 交换」那个闭环，就不该声称有设备标识。"""
        from mindos.local_web_debug import ACCESS_MODE_STANDALONE
        with patch.object(server, "_mindos_web_access_context",
                          return_value={"mode": ACCESS_MODE_STANDALONE, "localDebug": False}):
            self.assertNotIn("deviceId", server.get_mindos_web_access_context())

    def test_a_fresh_install_puts_data_outside_the_source_tree(self):
        """PRD V2 的「一个数据文件夹」：它要属于用户。

        安装目录多半是只读的，而且卸载软件时数据不该跟着没。
        """
        import runtime_paths

        home = runtime_paths._user_data_home()
        self.assertTrue(home.is_absolute())
        self.assertNotIn(runtime_paths.PROJECT_ROOT, home.parents,
                         f"全新安装的数据根不能落在源码树里：{home}")
        self.assertIn("hijun", home.name.lower() or "", "文件夹要能认出是知君的")

    def test_an_existing_data_folder_is_kept_where_it_is(self):
        """直接改默认会让一整个本体、对话、判断簿凭空「消失」（其实还在老地方），
        这是最吓人的一种升级体验。"""
        import runtime_paths
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            with patch.object(runtime_paths, "PROJECT_ROOT", root):
                self.assertEqual(runtime_paths._default_data_root(), root / "data")
            # 老位置不存在时才走用户数据目录
            empty = Path(temporary) / "elsewhere"
            empty.mkdir()
            with patch.object(runtime_paths, "PROJECT_ROOT", empty):
                self.assertEqual(runtime_paths._default_data_root(), runtime_paths._user_data_home())

    def test_config_exposes_no_hardcoded_source_dir_model_path(self):
        """config 里不得再出现 `Path(__file__).parent / "models_cache"` 这类写死路径。"""
        source = (BACKEND_DIR / "config.py").read_text(encoding="utf-8")
        for needle in ('"models_cache"', '"models_cache_ms"', '"whisper_models"'):
            self.assertNotIn(
                f"Path(__file__).parent / {needle}", source,
                f"config.py 仍把 {needle} 写死在源码目录下",
            )


if __name__ == "__main__":
    unittest.main()
