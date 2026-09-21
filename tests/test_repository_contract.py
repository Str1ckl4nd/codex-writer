"""Documentation and installation boundaries; no network or installed-app use."""
from pathlib import Path
import re
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent


class RepositoryContract(unittest.TestCase):
    def test_required_user_documents_are_present_and_linked(self):
        readme = (ROOT / 'README.md').read_text()
        for relative in ('PRIVACY.md', 'DISCLAIMER.md', 'NOTICE', 'LICENSE', 'docs/installation.md'):
            self.assertTrue((ROOT / relative).is_file())
            self.assertIn('](' + relative + ')', readme)
        self.assertIn('为什么要用它', readme)
        self.assertIn('明确适用的场景', readme)

    def test_local_markdown_links_resolve(self):
        documents = list(ROOT.glob('*.md')) + list((ROOT / 'docs').glob('*.md'))
        for document in documents:
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', document.read_text()):
                if '://' in target or target.startswith(('#', 'mailto:')):
                    continue
                relative = unquote(target.split('#', 1)[0])
                if relative:
                    self.assertTrue((document.parent / relative).exists(), str(document.name) + ': ' + target)

    def test_windows_storage_is_separate_and_ssh_requires_trusted_host_keys(self):
        windows = (ROOT / 'app/unlock.ps1').read_text(encoding='utf-8-sig')
        privacy = (ROOT / 'PRIVACY.md').read_text()
        self.assertIn("'SessionWriter\\logs'", windows)
        self.assertIn('%LOCALAPPDATA%\\SessionWriter\\logs\\', privacy)
        self.assertNotIn('CodexSessionWriter', windows)
        requests = [line for line in windows.splitlines() if '& ssh.exe ' in line]
        self.assertTrue(requests)
        for line in requests:
            self.assertIn('StrictHostKeyChecking=yes', line)

    def test_private_ci_has_no_automatic_push_trigger(self):
        workflow = (ROOT / '.github/workflows/check.yml').read_text()
        self.assertRegex(workflow, r'(?m)^on: \[workflow_dispatch\]$')

    def test_user_documents_are_visibility_independent(self):
        phrases=('private 开发预览版','此私有仓库','This repository is private',
                 'Current visibility: **private**','two-computer version','原个人工具')
        documents=list(ROOT.glob('*.md'))+list((ROOT/'docs').glob('*.md'))
        for document in documents:
            for phrase in phrases:
                self.assertNotIn(phrase,document.read_text(),document.name)


if __name__ == '__main__':
    unittest.main()
