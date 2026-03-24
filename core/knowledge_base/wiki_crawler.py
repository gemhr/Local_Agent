"""Wiki 内容抓取与落盘模块。"""

import os
import re
import time
from typing import Any, Dict, List

import requests


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
        payload = {"sn": sn}
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
            file_path = os.path.join(save_dir, f"{sn}_{safe_title}.pdf")
            self._download_binary_file(pdf_full_url, file_path)
            return

        file_path = os.path.join(save_dir, f"{sn}_{safe_title}.md")
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
        invalid_chars = r'[\\/:*?"<>|]'
        safe_name = re.sub(invalid_chars, "_", filename)
        return safe_name.strip()[:100]
