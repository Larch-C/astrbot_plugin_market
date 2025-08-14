import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import httpx
import jinja2
import markdown
from PIL import Image, UnidentifiedImageError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.t2i.renderer import HtmlRenderer

PLUGIN_API_URLS = [
    "https://api.soulter.top/astrbot/plugins",
    "https://plugin.astrbot.uk",
]
GITHUB_REPO_REGEX = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(\.git)?$")
PROXY_TEST_URL = "https://api.github.com"


@register(
    "astrbot_plugin_market",
    "长安某",
    "插件市场",
    "1.4.0",
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
            "render_endpoints", ["https://t2i.soulter.top/text2img"]
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
        """从配置动态读取渲染地址列表，并验证返回的是否为有效图片"""
        endpoints = self.config.get("render_endpoints", [])
        if not endpoints:
            raise RuntimeError("插件配置中未设置任何图片渲染地址")
        attempts = []
        for i, endpoint in enumerate(endpoints):
            address_number = i + 1
            endpoint_name = (
                "第一个地址 (主地址)"
                if address_number == 1
                else f"第{address_number}个地址 (备用)"
            )
            attempts.append((endpoint, endpoint_name))
        last_error = None
        for i, (endpoint, endpoint_name) in enumerate(attempts):
            try:
                logger.info(
                    f"开始渲染尝试 {i + 1}/{len(attempts)}：使用{endpoint_name} {endpoint}"
                )
                self.renderer.set_network_endpoint(endpoint)
                img_local_path = await self.renderer.render_custom_template(
                    html_content, data
                )
                if not img_local_path or not isinstance(img_local_path, str):
                    raise RuntimeError("渲染服务未返回有效的文件路径")
                logger.info("验证图片有效性...")
                try:
                    with open(img_local_path, "rb") as f:
                        image_data = f.read()
                except FileNotFoundError:
                    raise RuntimeError(
                        f"渲染器返回的路径无效或文件不存在: {img_local_path}"
                    )
                image_stream = BytesIO(image_data)
                try:
                    with Image.open(image_stream) as img:
                        img.verify()
                except UnidentifiedImageError:
                    error_message_preview = image_data.decode("utf-8", errors="ignore")[
                        :100
                    ]
                    raise RuntimeError(
                        f"文件内容不是有效的图片内容预览: '{error_message_preview}... '"
                    )
                except Exception as img_err:
                    raise RuntimeError(f"验证图像时发生未知错误: {img_err}")
                logger.info(f"成功使用 {endpoint_name} 渲染并验证为有效图片")
                return img_local_path
            except Exception as e:
                last_error = e
                logger.error(f"渲染尝试 {i + 1} ({endpoint_name}) 失败: {str(e)}")
                if i < len(attempts) - 1:
                    logger.warning("正在切换到下一个渲染地址...")
                    continue
        raise RuntimeError(
            f"所有渲染地址（共{len(attempts)}个）均失败最后一次错误: {last_error}"
        )

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
        """显示官方插件市场列表"""
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
        """根据关键词搜索插件"""
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
            yield event.plain_result(f"未找到包含 '{search_term}' 的插件")
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
                f"图片生成失败，搜索 '{search_term}' 共找到{total_matches}个结果"
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
        """通过编号、键名或URL安装插件"""
        arg = (
            event.message_str.strip().split()[1]
            if len(event.message_str.strip().split()) > 1
            else None
        )
        if not arg:
            yield event.plain_result("请指定要安装的插件编号、键名或GitHub仓库URL")
            return
        display_name = arg
        if not self._is_github_repo_url(arg):
            plugin_key = self._get_plugin_key_from_arg(arg)
            if plugin_key:
                display_name = plugin_key
        yield event.plain_result(f"开始安装插件: {display_name}...")
        try:
            repo_url = arg
            if not self._is_github_repo_url(arg):
                plugin_key = self._get_plugin_key_from_arg(arg)
                if not plugin_key:
                    yield event.plain_result(f"未在市场中找到插件: {arg}")
                    return
                plugin_info_market = self.plugins_data.get(plugin_key)
                if not plugin_info_market or not plugin_info_market.get("repo"):
                    yield event.plain_result(
                        f"插件 {plugin_key} 缺少仓库地址，无法安装"
                    )
                    return
                repo_url = plugin_info_market["repo"]
            installed_info = await self.plugin_manager.install_plugin(repo_url)
            if installed_info and installed_info.get("name"):
                plugin_name = installed_info["name"]
                yield event.plain_result(f"插件 '{plugin_name}' 安装并加载成功！")
                if installed_info.get("readme"):
                    try:
                        html_body = markdown.markdown(
                            installed_info["readme"],
                            extensions=["fenced_code", "tables"],
                        )
                        template = self.template_env.get_template(
                            "readme_template.html"
                        )
                        full_html = template.render(readme_body=html_body)
                        img_url = await self.render_with_fallback(full_html, {})
                        yield event.image_result(img_url)
                    except Exception as e:
                        logger.error(f"渲染插件 {plugin_name} 的README失败: {e}")
                        yield event.plain_result("(无法渲染插件的README文档)")
            else:
                yield event.plain_result(
                    f"插件 '{display_name}' 安装成功，但未获取到插件元信息，可能需要重启以确保功能正常"
                )
        except Exception as e:
            logger.error(f"安装插件 {display_name} 时发生错误: {e}", exc_info=True)
            yield event.plain_result(f"安装失败: {str(e)}")

    def _get_valid_installed_plugin_dirs(self) -> List[Path]:
        """获取并排序所有有效的本地插件目录"""
        if not self.plugins_dir.is_dir():
            return []
        valid_dirs = []
        for d in self.plugins_dir.iterdir():
            if d.is_dir() and not d.name.endswith("_backup"):
                if (d / "main.py").is_file():
                    valid_dirs.append(d)
        return sorted(valid_dirs, key=lambda p: p.name)

    @filter.command("已安装插件")
    async def show_installed_plugins(self, event: AstrMessageEvent):
        """显示本地已安装的插件列表，并生成独立的本地编号"""
        valid_plugin_dirs = self._get_valid_installed_plugin_dirs()
        if not valid_plugin_dirs:
            yield event.plain_result("当前未找到任何有效的已安装插件")
            return

        await self.fetch_plugin_data()
        args = event.message_str.strip().split()
        page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1

        plugin_items = []
        for i, plugin_dir in enumerate(valid_plugin_dirs):
            name = plugin_dir.name
            plugin_info = self._get_market_info_case_insensitive(name)
            if not plugin_info:
                plugin_info = {
                    "author": "未知",
                    "desc": "这是一个本地插件",
                    "stars": "N/A",
                    "updated_at": "",
                }

            plugin_items.append(
                {
                    "index": str(i + 1),
                    "key": name,
                    "author": str(plugin_info.get("author", "未知作者")),
                    "desc": str(plugin_info.get("desc", "无描述信息")),
                    "stars": plugin_info.get("stars", 0),
                    "updated_at": self._format_time(plugin_info.get("updated_at", "")),
                }
            )

        total_plugins = len(plugin_items)
        total_pages = (total_plugins + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_page_items = plugin_items[start_idx:end_idx]

        try:
            img_url = await self.render_plugin_list_image(
                plugins=current_page_items,
                total_items=total_plugins,
                page=page,
                total_pages=total_pages,
                title=f"已安装插件列表 (第{page}/{total_pages}页)",
                next_page_command=f"/已安装插件 {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"已安装插件列表图片生成失败: {str(e)}")
            yield event.plain_result(
                f"图片生成失败，当前第{page}/{total_pages}页，共{total_plugins}个已安装插件"
            )

    @filter.command("插件卸载")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def uninstall_plugin(self, event: AstrMessageEvent):
        """通过本地编号或插件名卸载插件"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("请输入要卸载的插件的【本地编号】或【文件夹名】")
            return
        arg = args[1]
        logger.info(f"接收到插件卸载指令，参数: '{arg}'")
        plugin_dir_name_to_uninstall = arg

        if arg.isdigit():
            logger.info(f"参数 '{arg}' 是一个数字，将尝试按本地编号解析...")
            valid_plugin_dirs = self._get_valid_installed_plugin_dirs()
            index = int(arg) - 1
            if 0 <= index < len(valid_plugin_dirs):
                plugin_dir_name_to_uninstall = valid_plugin_dirs[index].name
                logger.info(
                    f"本地编号 {arg} 解析为插件文件夹: '{plugin_dir_name_to_uninstall}'"
                )
            else:
                logger.warning(f"解析失败: 无效的本地编号 {arg}")
                yield event.plain_result(
                    f"无效的本地编号: {arg}请通过 /已安装插件 查看正确的编号"
                )
                return

        logger.info(f"查找文件夹为 '{plugin_dir_name_to_uninstall}' 的已加载插件...")
        plugin_to_uninstall = None
        for star in self.context.get_all_stars():
            if star.root_dir_name.lower() == plugin_dir_name_to_uninstall.lower():
                plugin_to_uninstall = star
                break

        if plugin_to_uninstall:
            registered_name = plugin_to_uninstall.name
            logger.info(f"已找到加载的插件 '{registered_name}'准备卸载...")
            try:
                yield event.plain_result(f"正在卸载插件: {registered_name}...")
                await self.plugin_manager.uninstall_plugin(plugin_name=registered_name)
                logger.info(f"成功卸载插件: {registered_name}")
                yield event.plain_result(f"插件 '{registered_name}' 已卸载成功")
            except Exception as e:
                logger.error(
                    f"在卸载插件 '{registered_name}' 时报告错误: {e}", exc_info=True
                )
                yield event.plain_result(f"卸载失败: {str(e)}")
        else:
            logger.warning(
                f"卸载失败：在已加载的插件中未找到与 '{plugin_dir_name_to_uninstall}' 匹配的插件"
            )
            yield event.plain_result(
                f"卸载失败：未找到名为 '{plugin_dir_name_to_uninstall}' 的已加载插件请检查名称或编号是否正确"
            )

    def _get_plugin_key_from_arg(self, arg: str) -> Optional[str]:
        """通过编号或键名，从市场数据中获取插件的唯一键名"""
        try:
            plugin_index = int(arg) - 1
            sorted_keys = list(self.plugins_data.keys())
            if 0 <= plugin_index < len(sorted_keys):
                return sorted_keys[plugin_index]
        except ValueError:
            lower_arg = arg.lower()
            for key in self.plugins_data:
                if key.lower() == lower_arg:
                    return key
        return None

    def _is_github_repo_url(self, url: str) -> bool:
        return bool(GITHUB_REPO_REGEX.match(url))

    def _find_readme_file(self, plugin_path: Path) -> Optional[Path]:
        """查找README.md文件"""
        if not plugin_path.is_dir():
            return None
        for file in plugin_path.iterdir():
            if file.name.lower() == "readme.md":
                return file
        return None

    def _get_market_info_case_insensitive(
        self, plugin_dir_name: str
    ) -> Optional[Dict[str, Any]]:
        """查找插件信息"""
        lower_dir_name = plugin_dir_name.lower()
        for key, info in self.plugins_data.items():
            if key.lower() == lower_dir_name:
                return info
        return None

    @filter.command("插件排行")
    async def show_plugin_ranking(self, event: AstrMessageEvent):
        """按Star数或更新时间查看插件排行"""
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
            yield event.plain_result("暂无插件数据可供排行")
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
                f"图片生成失败，当前按{sort_text}排序，第{page}/{total_pages}页"
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
