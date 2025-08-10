import asyncio
import re
import shutil
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zipfile import ZipFile

import aiohttp
import httpx
import jinja2
import markdown
from PIL import Image, UnidentifiedImageError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.t2i.renderer import HtmlRenderer

# 插件API地址（主地址和备用地址）
PLUGIN_API_URLS = [
    "https://api.soulter.top/astrbot/plugins",
    "https://plugin.astrbot.uk",
]

# GitHub仓库URL正则表达式
GITHUB_REPO_REGEX = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(\.git)?$")
# 用于测试代理连通性的稳定URL
PROXY_TEST_URL = "https://api.github.com"


@register(
    "astrbot_plugin_market",
    "长安某",
    "插件市场",
    "1.3.1",
    "https://github.com/zgojin/astrbot_plugin_market",
)
class PluginMarket(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.session = aiohttp.ClientSession()
        self.plugins_data = {}
        self.page_size = 10
        self.plugins_dir = Path("./data/plugins")
        self.plugin_manager = context._star_manager
        self.httpx_async_client = httpx.AsyncClient()
        endpoints = self.config.get(
            "render_endpoints", ["https://t2i.soulter.top/text2img"] # 会从配置中读取，但提供一个默认值，此地址并不会实际使用
        )
        self.renderer = HtmlRenderer(endpoints[0] if endpoints else "")

        self.template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=True,
        )

    async def on_load(self):
        await self.fetch_plugin_data()

    async def on_unload(self):
        if self.session:
            await self.session.close()
        if self.httpx_async_client:
            await self.httpx_async_client.aclose()

    async def fetch_plugin_data(self):
        """获取插件数据，支持多API地址重试"""
        for i, api_url in enumerate(PLUGIN_API_URLS):
            try:
                logger.info(
                    f"尝试从插件API地址 {i + 1}/{len(PLUGIN_API_URLS)} 获取数据: {api_url}"
                )
                async with self.session.get(api_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.plugins_data = {
                            k: v for k, v in data.items() if "repo" in v
                        }
                        logger.info(
                            f"成功从插件API地址 {i + 1} 获取到 {len(self.plugins_data)} 个插件数据"
                        )
                        return
                    else:
                        logger.warning(
                            f"从插件API地址 {i + 1} 获取数据失败，状态码: {response.status}"
                        )
            except Exception as e:
                logger.error(f"从插件API地址 {i + 1} 获取数据异常: {str(e)}")
                if i < len(PLUGIN_API_URLS) - 1:
                    logger.warning("正在尝试下一个插件API地址...")

        logger.error("所有插件API地址均无法获取数据")
        self.plugins_data = {}

    def sort_plugins(self, plugins):
        """按插件在plugins_data中的原始索引（实际编号）排序"""
        original_order = list(self.plugins_data.keys())
        return sorted(plugins.items(), key=lambda x: original_order.index(x[0]))

    async def render_with_fallback(self, html_content, data={}):
        """从配置动态读取渲染地址列表，并验证返回的是否为有效图片。"""
        endpoints = self.config.get("render_endpoints", [])
        if not endpoints:
            raise RuntimeError("插件配置中未设置任何图片渲染地址。")

        attempts = []
        for i, endpoint in enumerate(endpoints):
            address_number = i + 1
            if address_number == 1:
                endpoint_name = f"第一个地址 (主地址)"
            else:
                endpoint_name = f"第{address_number}个地址 (备用)"
            attempts.append((endpoint, endpoint_name))
        
        last_error = None
        for i, (endpoint, endpoint_name) in enumerate(attempts):
            try:
                logger.info(
                    f"开始渲染尝试 {i + 1}/{len(attempts)}：使用{endpoint_name} {endpoint}"
                )
                self.renderer.set_network_endpoint(endpoint)
                img_local_path = await self.renderer.render_custom_template(html_content, data)

                if not img_local_path or not isinstance(img_local_path, str):
                    raise RuntimeError("渲染服务未返回有效的文件路径。")

                logger.info(f"获取到本地图片路径: {img_local_path}，开始读取并验证其有效性...")
                
                try:
                    with open(img_local_path, "rb") as f:
                        image_data = f.read()
                except FileNotFoundError:
                    raise RuntimeError(f"渲染器返回的路径无效或文件不存在: {img_local_path}")

                image_stream = BytesIO(image_data)
                
                try:
                    with Image.open(image_stream) as img:
                        img.verify()
                except UnidentifiedImageError:
                    error_message_preview = image_data.decode('utf-8', errors='ignore')[:100]
                    raise RuntimeError(f"文件内容不是有效的图片。内容预览: '{error_message_preview}... '")
                except Exception as img_err:
                    raise RuntimeError(f"验证图像时发生未知错误: {img_err}")

                logger.info(f"成功使用 {endpoint_name} 渲染并验证为有效图片。")
                return img_local_path

            except Exception as e:
                last_error = e
                logger.error(f"渲染尝试 {i + 1} ({endpoint_name}) 失败: {str(e)}")
                if i < len(attempts) - 1:
                    logger.warning(f"正在切换到下一个渲染地址...")
                    continue
        
        raise RuntimeError(f"所有渲染地址（共{len(attempts)}个）均失败。最后一次错误: {last_error}")

    async def render_plugin_list_image(
        self,
        plugins: List[Dict[str, Any]],
        total_items: int,
        page: int,
        total_pages: int,
        title: str,
        is_search: bool = False,
        search_term: str = "",
        next_page_command: str = "",
    ) -> str:
        """渲染插件列表图片"""
        render_data = {
            "title": title,
            "is_search": is_search,
            "search_term": search_term,
            "total_items": total_items,
            "page": page,
            "total_pages": total_pages,
            "plugins": plugins,
            "has_next_page": page < total_pages,
            "next_page_command": next_page_command,
        }

        try:
            template = self.template_env.get_template("plugin_list_template.html")
            html_content = template.render(**render_data)
            return await self.render_with_fallback(html_content, {})
        except Exception as e:
            logger.error(f"模板渲染失败: {str(e)}")
            raise

    @filter.command("插件市场")
    async def show_plugin_market(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        args = event.message_str.strip().split()
        page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        total_plugins = len(self.plugins_data)

        if total_plugins == 0:
            try:
                img_url = await self.render_plugin_list_image(
                    [], 0, 1, 0, "AstrBot插件市场"
                )
                yield event.image_result(img_url)
            except:
                yield event.plain_result("暂无插件数据")
            return

        total_pages = (total_plugins + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))
        sorted_plugins = self.sort_plugins(self.plugins_data)
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_plugins = sorted_plugins[start_idx:end_idx]

        plugin_items = [
            {
                "index": list(self.plugins_data.keys()).index(plugin_key) + 1,
                "key": plugin_key,
                "author": str(plugin_info.get("author", "未标注作者")),
                "desc": str(plugin_info.get("desc", "无描述信息")),
                "stars": plugin_info.get("stars", 0),
                "updated_at": self._format_time(plugin_info.get("updated_at", "")),
            }
            for plugin_key, plugin_info in current_plugins
        ]

        try:
            img_url = await self.render_plugin_list_image(
                plugins=plugin_items,
                total_items=total_plugins,
                page=page,
                total_pages=total_pages,
                title=f"AstrBot插件市场 (第{page}/{total_pages}页)",
                next_page_command=f"/插件市场 {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"插件市场图片生成失败: {str(e)}")
            yield event.plain_result(
                f"图片生成失败，以下是插件列表：\n当前第{page}/{total_pages}页，共{total_plugins}个插件"
            )

    @filter.command("插件搜索")
    async def search_plugins(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()

        input_str = event.message_str.strip()
        search_part = input_str[4:].strip() if len(input_str) >= 4 else ""
        if not search_part:
            yield event.plain_result("请输入搜索关键词，例如：/插件搜索 天气")
            return

        parts = search_part.split()
        page = 1
        search_term = ""
        if parts and parts[-1].isdigit():
            try:
                page = int(parts.pop())
                search_term = " ".join(parts)
            except ValueError:
                search_term = search_part
        else:
            search_term = search_part

        if not search_term:
            yield event.plain_result("请输入搜索关键词，例如：/插件搜索 天气")
            return

        matched_plugins = self._filter_plugins_by_search_term(search_term)
        total_matches = len(matched_plugins)

        if total_matches == 0:
            yield event.plain_result(f"未找到包含 '{search_term}' 的插件。")
            return

        total_pages = (total_matches + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))
        sorted_matches = sorted(matched_plugins.items(), key=lambda x: x[0])
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_matches = sorted_matches[start_idx:end_idx]

        plugin_items = [
            {
                "index": list(self.plugins_data.keys()).index(plugin_key) + 1,
                "key": plugin_key,
                "author": str(plugin_info.get("author", "未标注作者")),
                "desc": str(plugin_info.get("desc", "无描述信息")),
                "stars": plugin_info.get("stars", 0),
                "updated_at": self._format_time(plugin_info.get("updated_at", "")),
            }
            for plugin_key, plugin_info in current_matches
        ]

        try:
            img_url = await self.render_plugin_list_image(
                plugins=plugin_items,
                total_items=total_matches,
                page=page,
                total_pages=total_pages,
                title=f"插件搜索结果 (第{page}/{total_pages}页)",
                is_search=True,
                search_term=search_term,
                next_page_command=f"/插件搜索 {search_term} {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"搜索结果图片生成失败: {str(e)}")
            yield event.plain_result(
                f"图片生成失败，搜索 '{search_term}' 共找到{total_matches}个结果。"
            )

    def _filter_plugins_by_search_term(self, term: str) -> Dict[str, dict]:
        if not term:
            return {}
        term_lower = term.lower()
        return {
            key: plugin
            for key, plugin in self.plugins_data.items()
            if term_lower in key.lower()
            or term_lower in str(plugin.get("desc", "")).lower()
            or term_lower in str(plugin.get("author", "")).lower()
        }

    @filter.command("插件安装")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def install_plugin(self, event: AstrMessageEvent):
        arg = (
            event.message_str.strip().split()[1]
            if len(event.message_str.strip().split()) > 1
            else None
        )
        if not arg:
            yield event.plain_result(
                "请指定要安装的插件编号、完整键名或GitHub仓库URL。"
            )
            return

        if self._is_github_repo_url(arg):
            yield event.plain_result("检测到GitHub仓库URL，准备从URL安装插件...")
            async for result in self._install_plugin_from_url(arg, event):
                yield result
            return

        plugin_key = self._get_plugin_key_from_arg(arg)
        if not plugin_key:
            yield event.plain_result(f"未找到插件: {arg}")
            return

        plugin_info = self.plugins_data.get(plugin_key)
        if not plugin_info or not plugin_info.get("repo"):
            yield event.plain_result(f"插件 {plugin_key} 缺少仓库地址，无法安装。")
            return

        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            yield event.plain_result(f"开始安装插件: {plugin_key}")
            await self.manage_plugin(
                plugin_key, plugin_info, self.plugins_dir, is_update=False
            )
            await self.load_plugin(plugin_key)
            yield event.plain_result(f"插件 {plugin_key} 安装并加载成功！")
            # 发送README文档
            async for readme_msg in self._send_readme_as_image(plugin_key, event):
                yield readme_msg

        except Exception as e:
            logger.error(f"安装插件 {plugin_key} 失败: {e}", exc_info=True)
            yield event.plain_result(f"安装失败: {str(e)}")

    def _get_plugin_key_from_arg(self, arg: str) -> Optional[str]:
        try:
            plugin_index = int(arg) - 1
            if 0 <= plugin_index < len(self.plugins_data):
                return list(self.plugins_data.keys())[plugin_index]
        except ValueError:
            return arg if arg in self.plugins_data else None
        return None

    def _is_github_repo_url(self, url: str) -> bool:
        return bool(GITHUB_REPO_REGEX.match(url))

    async def _install_plugin_from_url(self, repo_url: str, event: AstrMessageEvent):
        try:
            match = GITHUB_REPO_REGEX.match(repo_url)
            if not match:
                yield event.plain_result("无效的GitHub仓库URL格式。")
                return
            author, repo_name = match.group(1), match.group(2).replace(".git", "")
            plugin_info = {
                "repo": repo_url,
                "name": repo_name,
                "author": author,
                "desc": "从URL安装的插件",
            }
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            yield event.plain_result(f"开始从URL安装插件: {repo_name}")
            await self.manage_plugin(
                repo_name, plugin_info, self.plugins_dir, is_update=False
            )
            await self.load_plugin(repo_name)
            yield event.plain_result(f"插件 {repo_name} 从URL安装并加载成功！")
            # 发送README文档
            async for readme_msg in self._send_readme_as_image(repo_name, event):
                yield readme_msg

        except Exception as e:
            logger.error(f"从URL安装插件失败: {e}", exc_info=True)
            yield event.plain_result(f"安装失败: {str(e)}")

    async def manage_plugin(
        self, plugin_key: str, plugin: dict, plugins_dir: Path, is_update: bool = False
    ):
        target_path = plugins_dir / plugin_key
        backup_path = plugins_dir / f"{plugin_key}_backup" if is_update else None
        if is_update:
            if not target_path.exists():
                raise ValueError(f"插件 {plugin_key} 未安装，无法更新。")
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(target_path, backup_path)
        try:
            logger.info(f"正在从 {plugin['repo']} 下载插件 {plugin_key}...")
            await self.get_git_repo(plugin["repo"], target_path)
            logger.info(f"插件 {plugin_key} 下载成功。")
        except Exception as e:
            if is_update and backup_path and backup_path.exists():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.move(str(backup_path), str(target_path))
            elif not is_update and target_path.exists():
                shutil.rmtree(target_path)
            raise RuntimeError(f"下载插件 {plugin_key} 时出错: {e}") from e

    async def _test_proxy_latency(self, proxy: str) -> Tuple[str, float]:
        """测试代理的延迟（ms），失败则返回无限大延迟"""
        try:
            test_full_url = f"{proxy.rstrip('/')}/{PROXY_TEST_URL}"
            start_time = time.monotonic()
            async with httpx.AsyncClient() as client:
                await client.head(test_full_url, timeout=10.0, follow_redirects=False)
            latency = (time.monotonic() - start_time) * 1000
            logger.info(f"代理 {proxy} 测试成功，延迟: {latency:.2f} ms")
            return proxy, latency
        except Exception as e:
            logger.warning(f"代理 {proxy} 测试失败: {e}")
            return proxy, float("inf")

    async def _get_fastest_proxies(self) -> List[str]:
        """并发测试所有代理并按延迟排序"""
        configured_proxies = self.config.get("proxy_list", [])
        if not configured_proxies:
            return []

        logger.info("开始检测代理服务器连通性...")
        tasks = [self._test_proxy_latency(proxy) for proxy in configured_proxies]
        results = await asyncio.gather(*tasks)

        working_proxies = sorted(
            [res for res in results if res[1] != float("inf")], key=lambda x: x[1]
        )

        if not working_proxies:
            logger.warning("所有配置的代理服务器都无法连接。")
            return []

        logger.info(f"可用代理按速度排序: {[p[0] for p in working_proxies]}")
        return [p[0] for p in working_proxies]

    async def get_git_repo(self, url: str, target_path: Path):
        """从 Git 仓库下载插件"""
        temp_dir = Path(tempfile.mkdtemp())
        try:
            match = GITHUB_REPO_REGEX.match(url)
            if not match:
                raise ValueError("无效的GitHub仓库URL")
            author, repo = match.group(1), match.group(2).replace(".git", "")

            base_download_url = ""
            release_api_url = (
                f"https://api.github.com/repos/{author}/{repo}/releases/latest"
            )
            try:
                logger.info(f"正在检查最新发行版: {release_api_url}")
                async with httpx.AsyncClient() as client:
                    api_res = await client.get(
                        release_api_url, follow_redirects=True, timeout=15.0
                    )
                if api_res.status_code == 200:
                    release_data = api_res.json()
                    if "zipball_url" in release_data:
                        base_download_url = release_data["zipball_url"]
                        logger.info(
                            f"成功找到最新发行版: {release_data.get('tag_name', 'N/A')}"
                        )
                else:
                    logger.warning(
                        f"检查最新发行版失败 (状态码: {api_res.status_code})。将尝试下载默认分支。"
                    )
            except Exception as e:
                logger.warning(f"检查最新发行版时发生错误: {e}。将尝试下载默认分支。")

            if not base_download_url:
                base_download_url = (
                    f"https://github.com/{author}/{repo}/archive/HEAD.zip"
                )
                logger.info(f"使用默认分支下载地址: {base_download_url}")

            fastest_proxies = await self._get_fastest_proxies()
            attempt_prefixes = fastest_proxies + [""]

            zip_content = None
            last_error = None
            for prefix in attempt_prefixes:
                source = "代理" if prefix else "直连"
                download_url = (
                    f"{prefix.rstrip('/')}/{base_download_url}"
                    if prefix
                    else base_download_url
                )

                try:
                    logger.info(
                        f"开始尝试使用 ({source}: {prefix or '无'}) 下载: {download_url}"
                    )
                    response = await self.httpx_async_client.get(
                        download_url, follow_redirects=True, timeout=60.0
                    )
                    response.raise_for_status()
                    zip_content = BytesIO(response.content)
                    logger.info(f"成功使用 ({source}) 下载插件。")
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"使用 ({source}: {prefix or '无'}) 下载失败: {e}")
                    continue

            if not zip_content:
                raise RuntimeError(
                    f"所有代理及直连地址均尝试失败。最后一次错误: {last_error}"
                )

            with ZipFile(zip_content) as z:
                if not z.namelist():
                    raise RuntimeError("下载的压缩包为空。")

                root_dir_name = z.namelist()[0].split("/")[0]
                logger.info(f"正在解压插件到临时目录: {temp_dir}")
                z.extractall(temp_dir)

                source_path = temp_dir / root_dir_name
                if not source_path.is_dir():
                    raise RuntimeError(f"解压后未能找到预期的目录: {source_path}")

                logger.info(f"准备将 {source_path} 移动到 {target_path}")
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.move(str(source_path), str(target_path))
                logger.info("成功将插件移动到目标位置。")
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def load_plugin(self, plugin_name: str):
        try:
            await self.plugin_manager.load(specified_dir_name=plugin_name)
        except Exception as e:
            logger.warning(f"直接加载插件 {plugin_name} 失败: {e}，尝试重载。")
            try:
                await self.plugin_manager.reload(specified_plugin_name=plugin_name)
            except Exception as reload_err:
                logger.error(f"重载插件 {plugin_name} 也失败: {reload_err}")
                raise RuntimeError(f"加载插件失败: {e}, 重载也失败: {reload_err}")

    async def _send_readme_as_image(self, plugin_key: str, event: AstrMessageEvent):
        """发送插件的README文档"""
        plugin_path = self.plugins_dir / plugin_key
        readme_path = plugin_path / "README.md"

        if not readme_path.is_file():
            logger.warning(f"插件 {plugin_key} 中未找到 README.md 文件，跳过发送。")
            return

        try:
            logger.info(f"渲染插件 {plugin_key} 的 README.md.")
            readme_content = readme_path.read_text(encoding="utf-8")
            html_body = markdown.markdown(
                readme_content, extensions=["fenced_code", "tables"]
            )
            template = self.template_env.get_template("readme_template.html")
            full_html = template.render(readme_body=html_body)

            img_url = await self.render_with_fallback(full_html, {})
            yield event.image_result(img_url)

        except Exception as e:
            logger.error(f"渲染插件 {plugin_key} 的README失败: {e}")
            yield event.plain_result("(无法渲染插件的README文档)")

    @filter.command("插件排行")
    async def show_plugin_ranking(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        args = event.message_str.strip().split()
        sort_type = "star"
        if len(args) > 1:
            arg = args[1].lower()
            if arg in ["时间", "date", "updated"]:
                sort_type = "time"
            elif arg in ["star", "stars", "星级"]:
                sort_type = "star"

        page = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1

        if not self.plugins_data:
            yield event.plain_result("暂无插件数据可供排行。")
            return

        sorted_plugins = self._sort_plugins_by_type(sort_type)
        total_plugins = len(sorted_plugins)
        total_pages = (total_plugins + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_plugins = sorted_plugins[start_idx:end_idx]

        plugin_items = [
            {
                "index": list(self.plugins_data.keys()).index(plugin_key) + 1,
                "key": plugin_key,
                "author": str(plugin_info.get("author", "未标注作者")),
                "desc": str(plugin_info.get("desc", "无描述信息")),
                "stars": plugin_info.get("stars", 0),
                "updated_at": self._format_time(plugin_info.get("updated_at", "")),
            }
            for plugin_key, plugin_info in current_plugins
        ]

        sort_text = "更新时间" if sort_type == "time" else "Star数量"
        title = f"插件排行榜 (按{sort_text}排序, 第{page}/{total_pages}页)"

        try:
            img_url = await self.render_plugin_list_image(
                plugins=plugin_items,
                total_items=total_plugins,
                page=page,
                total_pages=total_pages,
                title=title,
                next_page_command=f"/插件排行 {sort_type} {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"插件排行图片生成失败: {str(e)}")
            yield event.plain_result(
                f"图片生成失败，当前按{sort_text}排序，第{page}/{total_pages}页。"
            )

    def _sort_plugins_by_type(self, sort_type: str) -> List[tuple]:
        """根据排序类型对插件进行排序"""
        if sort_type == "time":
            return sorted(
                self.plugins_data.items(),
                key=lambda x: x[1].get("updated_at", ""),
                reverse=True,
            )
        else:
            return sorted(
                self.plugins_data.items(),
                key=lambda x: x[1].get("stars", 0),
                reverse=True,
            )

    def _format_time(self, time_str: str) -> str:
        """格式化时间显示"""
        if not time_str:
            return "未知时间"
        try:
            if "T" in time_str and "Z" in time_str:
                return time_str.replace("T", " ").split(".")[0]
            from datetime import datetime

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(time_str, fmt).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            return time_str
        except Exception as e:
            logger.warning(f"时间格式解析失败: {time_str}, 错误: {e}")
            return time_str
