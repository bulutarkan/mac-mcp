import unittest

from tests._menu_sources import feature_source


class MenuPackageUpdateNoticeTests(unittest.TestCase):
    def test_package_install_update_check_is_decoded_before_git_info(self) -> None:
        models = feature_source("AppState")
        self.assertIn("struct PackageInstallUpdateNotice: Decodable", models)
        self.assertIn('case installKind = "install_kind"', models)
        self.assertIn('case downloadURL = "download_url"', models)
        state = feature_source("AppState")
        body = state[state.index("private func applyUpdateCheckOutput"):state.index("func checkForUpdates()")]
        self.assertLess(body.index("PackageInstallUpdateNotice.self"), body.index("UpdateCheckInfo.self"))
        self.assertIn('notice.installKind == "pkg"', body)
        self.assertIn("NSWorkspace.shared.open(url)", body)


if __name__ == "__main__":
    unittest.main()
