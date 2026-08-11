"""Wiki 内容抓取与落盘模块。"""

import os
import logging
import ntpath
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import requests


logger = logging.getLogger(__name__)
_WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class WikiCrawler:
    """抓取内部 Wiki 目录、文章内容和附件文件。"""

    BASE_URL = "https://besa.rnd.huawei.com"
    DIR_API = f"{BASE_URL}/wiki/api/directory/teamSpaceDirectory"
    CONTENT_API = f"{BASE_URL}/wiki/api/article/content"

    def __init__(self, cookie_str: str, output_base_dir: str) -> None:
        """初始化抓取器。

        Args:
            cookie_str: 内网登录 Cookie。
            output_base_dir: 抓取结果保存根目录。
        """
        self.output_base_dir = output_base_dir
        os.makedirs(self.output_base_dir, exist_ok=True)
        output_root = Path(self.output_base_dir).resolve(strict=True)
        if not output_root.is_dir():
            raise ValueError("Wiki output root 必须是 existing directory")
        self._output_root = output_root
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Cookie": cookie_str,
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

    def run_full_sync(self, start_space: int = 1, end_space: int = 45) -> None:
        """按空间编号范围执行全量同步。

        Args:
            start_space: 起始空间编号。
            end_space: 结束空间编号。
        """
        for index in range(start_space, end_space + 1):
            space_name = f"space{index:03d}"
            self._sync_single_space(space_name)
            time.sleep(1)

    def _sync_single_space(self, space_name: str) -> None:
        """同步单个空间下的全部文章。

        Args:
            space_name: 目标空间名称，例如 ``space001``。
        """
        payload = {"space_table_name": space_name}
        response = self.session.post(self.DIR_API, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            return

        directory_tree = data.get("data", {}).get("team_space_directory", [])
        if not directory_tree:
            return

        space_dir = os.path.join(self.output_base_dir, space_name)
        os.makedirs(space_dir, exist_ok=True)
        articles = self._extract_articles_from_tree(directory_tree)

        for sn, title in articles:
            self._fetch_and_save_article(sn, title, space_dir)
            time.sleep(0.5)

    def _extract_articles_from_tree(self, nodes: List[Dict[str, Any]]) -> List[tuple[str, str]]:
        """递归提取目录树中的文章节点。

        Args:
            nodes: 当前层级的目录节点列表。

        Returns:
            List[tuple[str, str]]: ``(sn, title)`` 元组列表。
        """
        articles = []
        for node in nodes:
            children = node.get("children", [])
            sn = node.get("sn")
            title = node.get("title", "untitled")
            if not children and sn:
                articles.append((sn, title))
            elif children:
                articles.extend(self._extract_articles_from_tree(children))
        return articles

    def _fetch_and_save_article(self, sn: str, title: str, save_dir: str) -> None:
        """抓取并保存单篇文章。

        Args:
            sn: 文章唯一标识。
            title: 文章标题。
            save_dir: 当前空间对应的保存目录。
        """
        safe_sn = self._validate_remote_leaf(sn)
        if safe_sn is None:
            self._log_security_skip("WIKI_REMOTE_FILENAME_INVALID")
            return
        payload = {"sn": safe_sn}
        safe_title = self._sanitize_filename(title)
        response = self.session.post(self.CONTENT_API, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            return

        content_text = data.get("data", {}).get("docContent", {}).get("content", "")
        if not content_text:
            return

        iframe_pattern = re.compile(r'<iframe src="(/wiki/api/attachment/[^"]+\.pdf)[^"]*".*?</iframe>')
        match = iframe_pattern.search(content_text)
        if match:
            pdf_relative_url = match.group(1)
            pdf_full_url = f"{self.BASE_URL}{pdf_relative_url}"
            file_path = self._contained_output_path(
                save_dir, f"{safe_sn}_{safe_title}.pdf"
            )
            if file_path is None:
                self._log_security_skip("WIKI_OUTPUT_PATH_DENIED")
                return
            self._download_binary_file(pdf_full_url, str(file_path))
            return

        file_path = self._contained_output_path(
            save_dir, f"{safe_sn}_{safe_title}.md"
        )
        if file_path is None:
            self._log_security_skip("WIKI_OUTPUT_PATH_DENIED")
            return
        with open(file_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(content_text)

    def _download_binary_file(self, url: str, save_path: str) -> None:
        """下载二进制附件。

        Args:
            url: 附件下载地址。
            save_path: 本地目标路径。
        """
        with self.session.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            with open(save_path, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=8192):
                    file_obj.write(chunk)

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """清洗文件名中的非法字符。

        Args:
            filename: 原始文件名。

        Returns:
            str: 可安全写入文件系统的文件名。
        """
        invalid_chars = r'[\\/:*?"<>|\x00-\x1f]'
        safe_name = re.sub(invalid_chars, "_", str(filename))
        safe_name = safe_name.strip().rstrip(". ")[:100].rstrip(". ")
        if not safe_name or safe_name in {".", ".."}:
            return "untitled"
        return safe_name

    @staticmethod
    def _validate_remote_leaf(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        leaf = value.strip()
        if (
            not leaf
            or leaf in {".", ".."}
            or leaf.endswith((".", " "))
            or any(ord(char) < 32 for char in leaf)
            or any(char in '<>:"/\\|?*' for char in leaf)
        ):
            return None
        basename = leaf.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            return None
        return leaf

    def _contained_output_path(self, save_dir: str, filename: str) -> Path | None:
        try:
            candidate = (Path(save_dir) / filename).resolve(strict=False)
            normalized_root = ntpath.normcase(ntpath.normpath(str(self._output_root)))
            normalized_candidate = ntpath.normcase(ntpath.normpath(str(candidate)))
            if ntpath.commonpath((normalized_root, normalized_candidate)) != normalized_root:
                return None
            if candidate.exists():
                resolved_existing = candidate.resolve(strict=True)
                normalized_existing = ntpath.normcase(
                    ntpath.normpath(str(resolved_existing))
                )
                if ntpath.commonpath((normalized_root, normalized_existing)) != normalized_root:
                    return None
            return candidate
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _log_security_skip(code: str) -> None:
        logger.warning(
            "Wiki article skipped by output security policy",
            extra={"safe_error_code": code, "component": "wiki_crawler"},
        )
