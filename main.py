import asyncio
import math
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional
from zipfile import ZipFile

import aiohttp
import click
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 插件API地址
PLUGIN_API_URL = "https://api.soulter.top/astrbot/plugins"
@register("astrbot_plugin_market", "长安某", "插件市场", "4.7.2","https://github.com/zgojin/astrbot_plugin_market")
class PluginMarket(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session = aiohttp.ClientSession()
        self.plugins_data = {}
        self.page_size = 10
        self.plugins_dir = Path("./data/plugins")
        self.proxy = context._config.get("proxy", None)
        self.loop = asyncio.get_event_loop()
        self.plugin_manager = context._star_manager
        # 新增异步httpx客户端
        self.httpx_async_client = None

    async def on_load(self):
        # 初始化异步httpx客户端
        self.httpx_async_client = httpx.AsyncClient(proxy=self.proxy)
        await self.fetch_plugin_data()

    async def on_unload(self):
        if self.session:
            await self.session.close()
        if self.httpx_async_client:
            await self.httpx_async_client.aclose()

    async def fetch_plugin_data(self):
        try:
            async with self.session.get(PLUGIN_API_URL) as response:
                if response.status == 200:
                    data = await response.json()
                    valid_plugins = {}
                    for key, plugin in data.items():
                        if "repo" in plugin:
                            valid_plugins[key] = plugin
                    self.plugins_data = valid_plugins
                else:
                    logger.error(f"获取插件数据失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"获取插件数据异常: {str(e)}")
            self.plugins_data = {}

    def sort_plugins(self, plugins):
        return sorted(plugins.items(), key=lambda x: x[0])

    @filter.command("插件市场")
    async def show_plugin_market(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        args = event.message_str.strip().split()
        page = 1
        if len(args) > 1 and args[1].isdigit():
            page = int(args[1])

        total_plugins = len(self.plugins_data)
        if total_plugins == 0:
            yield event.plain_result("暂无插件数据")
            return

        total_pages = math.ceil(total_plugins / self.page_size)
        page = max(1, min(page, total_pages))

        sorted_plugins = self.sort_plugins(self.plugins_data)
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        plugins = sorted_plugins[start_idx:end_idx]

        reply = f" AstrBot插件市场 (第{page}/{total_pages}页)\n\n"
        for i, (plugin_key, plugin_info) in enumerate(plugins, start=start_idx + 1):
            plugin_desc = plugin_info.get("desc", "无描述信息")
            author = plugin_info.get("author", "未标注作者")
            reply += f"{i}. **{plugin_key}**\n   - 作者: {author}\n   - 描述: {plugin_desc}\n"
            reply += f"   - 安装: /插件安装 {i}\n\n"

        if page < total_pages:
            reply += f"查看下一页: /插件市场 {page + 1}"

        yield event.plain_result(reply)

    @filter.command("插件搜索")
    async def search_plugins(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        search_term = event.message_str.strip()[4:].strip()
        if not search_term:
            yield event.plain_result("请输入搜索关键词")
            return

        matched_plugins = self._filter_plugins_by_search_term(search_term)
        total_matches = len(matched_plugins)

        if total_matches == 0:
            yield event.plain_result(f"未找到包含 '{search_term}' 的插件")
            return

        args = search_term.split()
        page = 1
        if len(args) > 1 and args[-1].isdigit():
            page = int(args.pop())
            search_term = " ".join(args)

        total_pages = math.ceil(total_matches / self.page_size)
        page = max(1, min(page, total_pages))

        sorted_matched_plugins = sorted(
            matched_plugins.items(), key=lambda item: item[0]
        )

        original_indices = [
            list(self.plugins_data.keys()).index(plugin_key) + 1
            for plugin_key, _ in sorted_matched_plugins
        ]

        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        page_plugins = sorted_matched_plugins[start_idx:end_idx]
        page_original_indices = original_indices[start_idx:end_idx]

        reply = f" 搜索结果 '{search_term}' (第{page}/{total_pages}页，共{total_matches}个)\n\n"
        for (plugin_key, plugin_info), original_index in zip(
            page_plugins, page_original_indices
        ):
            plugin_desc = plugin_info.get("desc", "无描述信息")
            author = plugin_info.get("author", "未标注作者")
            reply += f"{original_index}. **{plugin_key}**\n   - 作者: {author}\n   - 描述: {plugin_desc}\n"
            reply += f"   - 安装: /插件安装 {original_index}\n\n"

        if page < total_pages:
            reply += f"查看下一页: /插件搜索 {search_term} {page + 1}"

        yield event.plain_result(reply)

    def _filter_plugins_by_search_term(self, term: str) -> Dict[str, dict]:
        term_lower = term.lower()
        filtered = {}

        for key, plugin in self.plugins_data.items():
            key_match = term_lower in key.lower()
            desc_match = term_lower in (plugin.get("desc", "").lower() or "")
            author_match = term_lower in (plugin.get("author", "").lower() or "")

            if key_match or desc_match or author_match:
                filtered[key] = plugin

        return filtered

    @filter.command("插件安装")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def install_plugin(self, event: AstrMessageEvent):
        arg = (
            event.message_str.strip().split()[1]
            if len(event.message_str.strip().split()) > 1
            else None
        )

        if not arg:
            yield event.plain_result("请指定要安装的插件编号或完整键名")
            return

        plugin_info = None
        plugin_key = None

        try:
            plugin_index = int(arg) - 1
            if 0 <= plugin_index < len(self.plugins_data):
                plugin_key = list(self.plugins_data.keys())[plugin_index]
                plugin_info = self.plugins_data[plugin_key]
            else:
                if arg in self.plugins_data:
                    plugin_key = arg
                    plugin_info = self.plugins_data[plugin_key]
                else:
                    yield event.plain_result(f"未找到插件: {arg}")
                    return
        except ValueError:
            if arg in self.plugins_data:
                plugin_key = arg
                plugin_info = self.plugins_data[plugin_key]
            else:
                yield event.plain_result(f"未找到插件: {arg}")
                return

        if not plugin_info or not plugin_key:
            yield event.plain_result("获取插件信息失败，请确认插件存在")
            return

        plugin_name = plugin_key
        repo_url = plugin_info.get("repo")

        if not repo_url:
            yield event.plain_result(f"插件 {plugin_name} 缺少下载地址，无法安装")
            return

        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            yield event.plain_result(f" 开始安装插件: {plugin_name}")

            # 执行插件下载和安装 - 改为直接异步调用
            await self.manage_plugin(
                plugin_key,
                plugin_info,
                self.plugins_dir,
                is_update=False,
                proxy=self.proxy,
            )

            # 安装成功后自动加载新插件
            await self.load_new_plugin(plugin_name)
            yield event.plain_result(f" 插件 {plugin_name} 安装并加载成功！")

        except Exception as e:
            logger.error(f"安装插件 {plugin_name} 失败: {e}", exc_info=True)
            yield event.plain_result(f" 安装失败: {str(e)}")

    async def manage_plugin(
        self,
        plugin_key: str,
        plugin: dict,
        plugins_dir: Path,
        is_update: bool = False,
        proxy: Optional[str] = None,
    ) -> None:
        plugin_name = plugin_key
        repo_url = plugin["repo"]
        target_path = plugins_dir / plugin_name
        backup_path = Path(f"{target_path}_backup") if is_update else None

        if is_update and not target_path.exists():
            raise click.ClickException(f"插件 {plugin_name} 未安装，无法更新")

        if is_update and backup_path.exists():
            shutil.rmtree(backup_path)
        if is_update:
            shutil.copytree(target_path, backup_path)

        try:
            print(f"正在从 {repo_url} 下载插件 {plugin_name}...")
            await self.get_git_repo(repo_url, target_path, proxy)
            print(f"插件 {plugin_name} 安装成功")
        except Exception as e:
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            if is_update and backup_path.exists():
                shutil.move(backup_path, target_path)
            raise click.ClickException(f"安装插件 {plugin_name} 时出错: {e}")

    async def get_git_repo(self, url: str, target_path: Path, proxy: Optional[str] = None):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            repo_namespace = url.split("/")[-2:]
            author, repo = repo_namespace[0], repo_namespace[1]
            release_url = f"https://api.github.com/repos/{author}/{repo}/releases"

            try:
                # 使用异步httpx请求获取版本信息
                response = await self.httpx_async_client.get(release_url)
                response.raise_for_status()
                releases = response.json()
                download_url = (
                    releases[0]["zipball_url"]
                    if releases
                    else f"https://github.com/{author}/{repo}/archive/refs/heads/master.zip"
                )
            except Exception:
                download_url = url

            if proxy:
                download_url = f"{proxy}/{download_url}"

            # 下载插件zip包 - 异步请求
            response = await self.httpx_async_client.get(download_url)
            if response.status_code == 404 and "master.zip" in download_url:
                alt_url = download_url.replace("master.zip", "main.zip")
                print("尝试下载main分支")
                response = await self.httpx_async_client.get(alt_url)
                response.raise_for_status()
            else:
                response.raise_for_status()
            zip_content = BytesIO(response.content)

            with ZipFile(zip_content) as z:
                z.extractall(temp_dir)
                root_dir = Path(z.namelist()[0]).parts[0] if z.namelist() else ""
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.move(temp_dir / root_dir, target_path)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def load_new_plugin(self, plugin_name: str):
        """加载新安装的插件"""
        try:
            # 通过插件目录名加载插件
            await self.plugin_manager.load(specified_dir_name=plugin_name)
        except Exception as e:
            logger.error(f"加载插件 {plugin_name} 失败: {e}", exc_info=True)
            # 尝试通过重载方式加载
            try:
                await self.plugin_manager.reload(specified_plugin_name=plugin_name)
            except Exception as reload_err:
                logger.error(
                    f"重载插件 {plugin_name} 失败: {reload_err}", exc_info=True
                )
                raise Exception(f"加载插件失败: {e}, 重载尝试也失败: {reload_err}")

    async def terminate(self):
        await self.on_unload()
