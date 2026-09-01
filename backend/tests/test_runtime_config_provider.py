"""runtime_config_provider 单元测试（P1 §5.1.1 快照 / §5.2.1 secret saga）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from mindos import runtime_config_provider as rcp
from mindos.secret_store import MemorySecretStore, UnavailableSecretStore
from mindos.stores.runtime_settings_store import reset_for_tests

_QA_URL = "http://127.0.0.1:8000/v1"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp.name) / "runtime_settings.db")
        self.store = reset_for_tests(self._db)
        self.secrets = MemorySecretStore()
        self.provider = rcp.RuntimeConfigProvider(
            store=self.store, secret_store=self.secrets
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()


class DefaultsTest(_Base):
    def test_default_snapshot_matches_config(self) -> None:
        local = self.provider.get_local_snapshot()
        self.assertEqual(local.base_url, config.LOCAL_OLLAMA_URL)
        self.assertEqual(local.model, config.LOCAL_OLLAMA_MODEL)
        self.assertEqual(local.timeout_seconds, config.RECOGNITION_AI_TIMEOUT_SECONDS)

    def test_empty_status(self) -> None:
        status = self.provider.section_status(rcp.SECTION_MATERIAL)
        self.assertEqual(status["revision"], 0)
        self.assertEqual(status["source"], "defaults")


class MaterialSaveTest(_Base):
    def test_save_updates_snapshot_and_status(self) -> None:
        row = self.provider.save_material_runtime(
            base_url="http://127.0.0.1:11434",
            model="qwen3:4b",
            timeout_seconds=120,
            expected_revision=None,
        )
        self.assertEqual(row["revision"], 1)
        local = self.provider.get_local_snapshot()
        self.assertEqual(local.model, "qwen3:4b")
        status = self.provider.section_status(rcp.SECTION_MATERIAL)
        self.assertEqual(status["source"], "runtime_settings")

    def test_invalid_urls_rejected(self) -> None:
        for url in (
            "ftp://127.0.0.1",
            "http://127.0.0.1:11434/api/chat",
            "http://user:pw@127.0.0.1:11434",
            "http://127.0.0.1:11434?x=1",
            "http://127.0.0.1:11434/v1",
            "http://127.0.0.1:11434//",
            "http://127.0.0.1:11434/../x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(rcp.ValidationError):
                    self.provider.save_material_runtime(
                        base_url=url,
                        model="m",
                        timeout_seconds=180,
                        expected_revision=None,
                    )

    def test_model_and_timeout_validation(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_material_runtime(
                base_url="http://127.0.0.1:11434",
                model="bad model!",
                timeout_seconds=180,
                expected_revision=None,
            )
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_material_runtime(
                base_url="http://127.0.0.1:11434",
                model="m",
                timeout_seconds=5,
                expected_revision=None,
            )

    def test_remote_address_is_accepted(self) -> None:
        self.provider.save_material_runtime(
            base_url="http://10.1.2.3:11434",
            model="m",
            timeout_seconds=180,
            expected_revision=None,
        )
        self.assertEqual(self.provider.get_local_snapshot().base_url, "http://10.1.2.3:11434")

    def test_missing_revision_on_existing_conflicts(self) -> None:
        self.provider.save_material_runtime(
            base_url="http://127.0.0.1:11434",
            model="m",
            timeout_seconds=180,
            expected_revision=None,
        )
        with self.assertRaises(rcp.RevisionConflictError):
            self.provider.save_material_runtime(
                base_url="http://127.0.0.1:11434",
                model="m2",
                timeout_seconds=180,
                expected_revision=None,  # 已存在配置：缺失 revision 必须冲突，不能静默覆盖
            )


class ChatSaveTest(_Base):
    def _openai(self, **kw) -> dict:
        base = dict(
            provider="openai",
            external_enabled=True,
            base_url=_QA_URL,
            model="deepseek-chat",
            timeout_seconds=60,
            total_budget_seconds=90,
            fallback_ollama=True,
            expected_revision=None,
        )
        base.update(kw)
        return base

    def test_save_with_api_key(self) -> None:
        row = self.provider.save_chat_provider(api_key="sk-test", **self._openai())
        self.assertEqual(row["revision"], 1)
        snap = self.provider.get_chat_snapshot()
        self.assertTrue(snap.api_key_configured)
        self.assertEqual(self.provider.resolve_api_key(snap), "sk-test")
        self.assertEqual(snap.base_url, _QA_URL)

    def test_keep_key_when_omitted(self) -> None:
        self.provider.save_chat_provider(api_key="sk-a", **self._openai())
        self.provider.save_chat_provider(
            api_key=None, **self._openai(expected_revision=1)
        )
        snap = self.provider.get_chat_snapshot()
        self.assertEqual(self.provider.resolve_api_key(snap), "sk-a")

    def test_clear_key(self) -> None:
        self.provider.save_chat_provider(api_key="sk-a", **self._openai())
        # 清密钥必须先关闭外发（启用外部必须配置 API Key 的不变量）
        self.provider.save_chat_provider(
            clear_api_key=True, **self._openai(expected_revision=1, external_enabled=False)
        )
        snap = self.provider.get_chat_snapshot()
        self.assertFalse(snap.api_key_configured)
        self.assertFalse(snap.external_enabled)
        self.assertIsNone(self.provider.resolve_api_key(snap))

    def test_stale_revision_compensates_new_secret(self) -> None:
        self.provider.save_chat_provider(api_key="sk-a", **self._openai())
        before = set(self.secrets.list_secret_refs())
        with self.assertRaises(rcp.RevisionConflictError):
            self.provider.save_chat_provider(
                api_key="sk-new", **self._openai(expected_revision=0)
            )
        after = set(self.secrets.list_secret_refs())
        self.assertEqual(before, after)  # 新密钥被补偿删除

    def test_store_failure_compensates_new_secret(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("db down")

        with patch.object(self.store, "put_section", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.provider.save_chat_provider(
                    api_key="sk-new", **self._openai()
                )
        self.assertEqual(list(self.secrets.list_secret_refs()), [])

    def test_qa_url_with_endpoint_rejected(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_chat_provider(
                **self._openai(base_url="http://127.0.0.1:8000/v1/chat/completions")
            )

    def test_qa_base_with_query_rejected(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_chat_provider(
                **self._openai(base_url="http://127.0.0.1:8000/v1?key=x")
            )

    def test_remote_chat_address_is_accepted(self) -> None:
        self.provider.save_chat_provider(
            api_key="sk-test", **self._openai(base_url="http://10.1.2.3/v1")
        )
        self.assertEqual(self.provider.get_chat_snapshot().base_url, "http://10.1.2.3/v1")

    def test_ollama_provider_ignores_url_and_key(self) -> None:
        self.provider.save_chat_provider(
            provider="ollama",
            external_enabled=False,
            base_url="",
            model=None,
            timeout_seconds=60,
            total_budget_seconds=90,
            fallback_ollama=True,
            expected_revision=None,
        )
        snap = self.provider.get_chat_snapshot()
        self.assertEqual(snap.provider, "ollama")
        self.assertIsNone(snap.base_url)

    def test_openai_enabled_without_key_rejected(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_chat_provider(**self._openai(api_key=None))

    def test_clear_key_while_enabled_rejected(self) -> None:
        self.provider.save_chat_provider(api_key="sk-a", **self._openai())
        with self.assertRaises(rcp.ValidationError):
            self.provider.save_chat_provider(
                clear_api_key=True, **self._openai(expected_revision=1)
            )

    def test_ollama_provider_forces_external_off(self) -> None:
        self.provider.save_chat_provider(
            provider="ollama",
            external_enabled=True,  # 矛盾输入：ollama 强制关闭外发
            base_url="",
            model=None,
            timeout_seconds=60,
            total_budget_seconds=90,
            fallback_ollama=True,
            expected_revision=None,
        )
        row = self.store.get_section(rcp.SECTION_CHAT)
        self.assertFalse(row["payload"]["externalEnabled"])


class CandidateTest(_Base):
    def test_material_candidate_not_persisted(self) -> None:
        snap = self.provider.candidate_local_snapshot(
            base_url="http://127.0.0.1:11434", model="qwen3:8b", timeout_seconds=120
        )
        self.assertEqual(snap.model, "qwen3:8b")
        self.assertNotEqual(self.provider.get_local_snapshot().model, snap.model)
        self.assertIsNone(self.store.get_section(rcp.SECTION_MATERIAL))

    def test_remote_material_candidate_is_accepted(self) -> None:
        snap = self.provider.candidate_local_snapshot(
            base_url="http://10.1.2.3:11434",
            model="qwen3:8b",
            timeout_seconds=120,
        )
        self.assertEqual(snap.base_url, "http://10.1.2.3:11434")

    def test_candidate_invalid_rejected(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.candidate_local_snapshot(
                base_url="http://127.0.0.1:11434/api/chat"
            )

    def test_chat_candidate_with_key(self) -> None:
        snap = self.provider.candidate_chat_snapshot(
            provider="openai",
            base_url=_QA_URL,
            model="deepseek-chat",
            api_key="sk-candidate",
        )
        self.assertEqual(
            self.provider.resolve_candidate_api_key(snap, "sk-candidate"), "sk-candidate"
        )

    def test_chat_candidate_external_and_fallback_params(self) -> None:
        snap = self.provider.candidate_chat_snapshot(
            provider="openai",
            base_url=_QA_URL,
            model="deepseek-chat",
            external_enabled=True,
            fallback_ollama=False,
            api_key="sk-candidate",
        )
        self.assertTrue(snap.external_enabled)
        self.assertFalse(snap.fallback_ollama)

    def test_chat_candidate_enabled_without_key_rejected(self) -> None:
        with self.assertRaises(rcp.ValidationError):
            self.provider.candidate_chat_snapshot(
                provider="openai", base_url=_QA_URL, model="m", external_enabled=True
            )


class ChatLocalLinkTest(_Base):
    def test_chat_local_follows_material_update(self) -> None:
        self.provider.save_chat_provider(
            provider="ollama",
            external_enabled=False,
            base_url="",
            model=None,
            timeout_seconds=60,
            total_budget_seconds=90,
            fallback_ollama=True,
            expected_revision=None,
        )
        self.provider.save_material_runtime(
            base_url="http://127.0.0.1:11434",
            model="qwen3:8b",
            timeout_seconds=120,
            expected_revision=None,
        )
        snap = self.provider.get_chat_snapshot()
        self.assertEqual(snap.local.model, "qwen3:8b")
        self.assertEqual(snap.local.base_url, "http://127.0.0.1:11434")


class UnavailableSecretTest(_Base):
    def test_save_key_raises_when_secret_store_unavailable(self) -> None:
        prov = rcp.RuntimeConfigProvider(
            store=self.store, secret_store=UnavailableSecretStore()
        )
        with self.assertRaises(rcp.RuntimeConfigError):
            prov.save_chat_provider(
                provider="openai",
                external_enabled=True,
                base_url=_QA_URL,
                model="m",
                timeout_seconds=60,
                total_budget_seconds=90,
                fallback_ollama=True,
                api_key="sk-x",
                expected_revision=None,
            )


class OrphanCleanupTest(_Base):
    def _save_openai(self, api_key: str | None = "sk-a") -> None:
        self.provider.save_chat_provider(
            provider="openai",
            external_enabled=True,
            base_url=_QA_URL,
            model="m",
            timeout_seconds=60,
            total_budget_seconds=90,
            fallback_ollama=True,
            api_key=api_key,
            expected_revision=None,
        )

    def test_ledger_based_cleanup(self) -> None:
        self._save_openai()
        referenced = self.provider.get_chat_snapshot().secret_ref
        self.assertIsNotNone(referenced)
        orphan = "mindos_rt_orphan"
        self.store.add_secret_ref(orphan, created_at=time.time() - 100)
        self.secrets.set_secret(orphan, "x")
        removed = self.provider.cleanup_orphan_secrets(retention_seconds=0)
        self.assertIn(orphan, removed)
        self.assertNotIn(referenced, removed)
        self.assertIsNone(self.secrets.get_secret(orphan))
        self.assertEqual(
            self.provider.resolve_api_key(self.provider.get_chat_snapshot()), "sk-a"
        )

    def test_orphan_within_retention_kept(self) -> None:
        self._save_openai()
        orphan = "mindos_rt_fresh"
        self.store.add_secret_ref(orphan, created_at=time.time())
        self.secrets.set_secret(orphan, "x")
        removed = self.provider.cleanup_orphan_secrets(retention_seconds=3600)
        self.assertNotIn(orphan, removed)
        self.assertEqual(self.secrets.get_secret(orphan), "x")


if __name__ == "__main__":
    unittest.main()
