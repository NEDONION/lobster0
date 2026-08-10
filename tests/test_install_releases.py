"""验证 fixed、stable 和 bounded dev Release 来源发现。"""

from __future__ import annotations

import io
import json
import unittest
from urllib.request import Request

from lobster0.install.models import InstallError
from lobster0.install.releases import ReleaseSource, resolve_release_source


class FakeResponse(io.BytesIO):
    """模拟 GitHub API 的离线 HTTP response。"""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """保存 body、status 与 headers。"""
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def getcode(self) -> int:
        """返回 HTTP status。"""
        return self.status


class FakeOpener:
    """按顺序返回 GitHub API 假响应。"""

    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        """保存响应队列与请求 URL。"""
        self.responses = list(responses)
        self.urls: list[str] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        """记录请求并返回下一响应。"""
        del timeout
        self.urls.append(request.full_url)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def release(tag: str, *, draft: bool = False, prerelease: bool = True) -> dict[str, object]:
    """构造字段与真实 GitHub Release response 一致的最小记录。"""
    return {
        "tag_name": f"v{tag}",
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/NEDONION/lobster0/releases/tag/v{tag}",
        "assets": [
            {
                "name": "release-manifest.json",
                "browser_download_url": (
                    "https://github.com/NEDONION/lobster0/releases/download/"
                    f"v{tag}/release-manifest.json"
                ),
            }
        ],
    }


class InstallReleaseTests(unittest.TestCase):
    """覆盖 release source 收窄与 dev semver discovery。"""

    def test_fixed_and_stable_sources_are_exact_and_do_not_fetch(self) -> None:
        """错误 tag/latest path 或提前网络请求都会破坏 Release 信任入口。"""
        opener = FakeOpener()

        fixed = resolve_release_source("stable", "0.7.0", opener=opener)
        stable = resolve_release_source("stable", None, opener=opener)

        self.assertEqual(
            fixed,
            ReleaseSource(
                "stable",
                "0.7.0",
                (
                    "https://github.com/NEDONION/lobster0/releases/download/"
                    "v0.7.0/release-manifest.json"
                ),
                None,
            ),
        )
        self.assertEqual(
            stable.manifest_url,
            "https://github.com/NEDONION/lobster0/releases/latest/download/release-manifest.json",
        )
        self.assertEqual(opener.urls, [])

    def test_stable_rejects_prerelease_and_dev_requires_prerelease(self) -> None:
        """stable/dev 通道不能接受相反稳定性的显式版本。"""
        for channel, version in (
            ("stable", "0.8.0-rc.1"),
            ("dev", "0.8.0"),
            ("stable", "v0.7.0"),
            ("nightly", "0.7.0"),
        ):
            with self.subTest(channel=channel, version=version), self.assertRaisesRegex(
                InstallError,
                "manifest_invalid",
            ):
                resolve_release_source(channel, version)  # type: ignore[arg-type]

    def test_explicit_and_dev_api_reject_huge_numeric_semver_with_stable_error(self) -> None:
        """超长 numeric identifier 不得让 int 限制的 raw ValueError 穿出边界。"""
        oversized_identifier = "9" * 19
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            resolve_release_source("stable", f"{oversized_identifier}.0.0")

        huge = "9" * 5000
        with self.assertRaises(InstallError) as explicit:
            resolve_release_source("stable", f"{huge}.0.0")
        self.assertEqual(explicit.exception.code, "manifest_invalid")

        body = json.dumps([release(f"{huge}.0.0-rc.1")]).encode("utf-8")
        with self.assertRaises(InstallError) as discovered:
            resolve_release_source("dev", None, opener=FakeOpener(FakeResponse(body)))
        self.assertEqual(discovered.exception.code, "manifest_invalid")

    def test_dev_discovers_highest_prerelease_from_exact_bounded_api(self) -> None:
        """draft/stable 排除或字典序排序错误都会选择错误的 dev Release。"""
        rows = [
            release("0.9.0-rc.1", draft=True),
            release("0.10.0", prerelease=False),
            release("0.8.0-rc.2"),
            release("0.8.0-rc.10"),
            release("0.8.0-beta.9"),
        ]
        body = json.dumps(rows).encode("utf-8")
        opener = FakeOpener(FakeResponse(body, headers={"Content-Length": str(len(body))}))

        source = resolve_release_source("dev", None, opener=opener)

        self.assertEqual(
            opener.urls,
            ["https://api.github.com/repos/NEDONION/lobster0/releases?per_page=20"],
        )
        self.assertEqual(source.channel, "dev")
        self.assertEqual(source.requested_version, "0.8.0-rc.10")
        self.assertEqual(
            source.manifest_url,
            (
                "https://github.com/NEDONION/lobster0/releases/download/"
                "v0.8.0-rc.10/release-manifest.json"
            ),
        )

    def test_dev_rejects_oversized_malformed_duplicate_and_unbounded_api_json(self) -> None:
        """API byte/row/JSON 边界必须在选择 Release 前 fail closed。"""
        cases = (
            FakeResponse(b"[", headers={"Content-Length": "1"}),
            FakeResponse(b"[]" + b" " * 1_048_575),
            FakeResponse(json.dumps([release("0.8.0-rc.1")] * 21).encode("utf-8")),
            FakeResponse(
                b'[{"tag_name":"v0.8.0-rc.1","tag_name":"v9.0.0-rc.1",'
                b'"draft":false,"prerelease":true,"html_url":"x","assets":[]}]'
            ),
        )
        for index, response in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(InstallError) as caught:
                resolve_release_source("dev", None, opener=FakeOpener(response))
            self.assertEqual(caught.exception.code, "manifest_invalid")

    def test_dev_rejects_wrong_repository_asset_name_url_and_duplicate_manifest(self) -> None:
        """dev Release 必须绑定 exact repository、tag 和唯一 manifest asset。"""
        mutations: list[dict[str, object]] = []
        wrong_repository = release("0.8.0-rc.1")
        wrong_repository["html_url"] = "https://github.com/attacker/lobster0/releases/tag/v0.8.0-rc.1"
        mutations.append(wrong_repository)
        wrong_name = release("0.8.0-rc.1")
        wrong_name["assets"] = [{"name": "manifest.json", "browser_download_url": "https://example.com"}]
        mutations.append(wrong_name)
        wrong_url = release("0.8.0-rc.1")
        wrong_url["assets"] = [
            {
                "name": "release-manifest.json",
                "browser_download_url": (
                    "https://github.com/attacker/lobster0/releases/download/"
                    "v0.8.0-rc.1/release-manifest.json"
                ),
            }
        ]
        mutations.append(wrong_url)
        duplicate = release("0.8.0-rc.1")
        duplicate["assets"] = [*duplicate["assets"], *duplicate["assets"]]  # type: ignore[index]
        mutations.append(duplicate)

        for row in mutations:
            body = json.dumps([row]).encode("utf-8")
            with self.subTest(row=row), self.assertRaisesRegex(InstallError, "manifest_invalid"):
                resolve_release_source("dev", None, opener=FakeOpener(FakeResponse(body)))

    def test_dev_rejects_duplicate_release_tags(self) -> None:
        """重复 tag 不得依赖远端数组顺序静默选择一个来源。"""
        body = json.dumps(
            [release("0.8.0-rc.1"), release("0.8.0-rc.1")]
        ).encode("utf-8")

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            resolve_release_source("dev", None, opener=FakeOpener(FakeResponse(body)))

    def test_dev_rejects_redirect_from_api_to_asset_credentials_query_or_fragment(self) -> None:
        """API origin 不得借 redirect 获得 asset host 权限或携带不可信 URL parts。"""
        locations = (
            "https://release-assets.githubusercontent.com/api-confusion",
            "https://user:pass@api.github.com/repos/NEDONION/lobster0/releases?per_page=20",
            "https://api.github.com/repos/NEDONION/lobster0/releases?per_page=19",
            "https://api.github.com/repos/NEDONION/lobster0/releases?per_page=20#fragment",
        )
        for location in locations:
            with self.subTest(location=location), self.assertRaises(InstallError) as caught:
                resolve_release_source(
                    "dev",
                    None,
                    opener=FakeOpener(
                        FakeResponse(
                            b"SECRET_API_BODY",
                            status=302,
                            headers={"Location": location},
                        )
                    ),
                )
            self.assertEqual(caught.exception.code, "manifest_invalid")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_release_source_constructor_rejects_untrusted_fields(self) -> None:
        """调用方不能绕过 resolver 构造任意 manifest URL 或 hash。"""
        latest = (
            "https://github.com/NEDONION/lobster0/releases/latest/download/"
            "release-manifest.json"
        )
        for values in (
            ("nightly", None, latest, None),
            ("stable", "v0.7.0", latest, None),
            ("stable", None, "https://evil.example/release-manifest.json", None),
            ("stable", None, f"{latest}?x=1", None),
            ("stable", None, latest, "A" * 64),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                InstallError,
                "manifest_invalid",
            ):
                ReleaseSource(*values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
