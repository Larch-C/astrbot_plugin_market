import re
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from zipfile import ZipFile

import aiohttp
import httpx
import jinja2
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 导入 HtmlRenderer
from astrbot.core.utils.t2i.renderer import HtmlRenderer

# 插件API地址（主地址和备用地址）
PLUGIN_API_URLS = [
    "https://api.soulter.top/astrbot/plugins",  
    "https://plugin.astrbot.uk"                 
]

# GitHub仓库URL正则表达式
GITHUB_REPO_REGEX = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(\.git)?$")


@register(
    "astrbot_plugin_market",
    "长安某",
    "插件市场",
    "1.2.1",
    "https://github.com/zgojin/astrbot_plugin_market",
)
class PluginMarket(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session = aiohttp.ClientSession()
        self.plugins_data = {}
        self.page_size = 10
        self.plugins_dir = Path("./data/plugins")
        self.proxy = context._config.get("proxy", None)
        self.plugin_manager = context._star_manager
        self.httpx_async_client = httpx.AsyncClient(proxy=self.proxy)
        
        # 渲染配置
        self.render_endpoint = "https://t2i.soulter.top/text2img"  
        self.fallback_render_endpoint = "https://t2i.astrbot.uk"
        self.renderer = HtmlRenderer(self.render_endpoint)
        
        # 初始化Jinja2模板环境
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
                logger.info(f"尝试从插件API地址 {i+1}/{len(PLUGIN_API_URLS)} 获取数据: {api_url}")
                async with self.session.get(api_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        valid_plugins = {}
                        for key, plugin in data.items():
                            if "repo" in plugin:
                                valid_plugins[key] = plugin
                        self.plugins_data = valid_plugins
                        logger.info(f"成功从插件API地址 {i+1} 获取到 {len(valid_plugins)} 个插件数据")
                        return
                    else:
                        logger.warning(f"从插件API地址 {i+1} 获取数据失败，状态码: {response.status}")
            except Exception as e:
                logger.error(f"从插件API地址 {i+1} 获取数据异常: {str(e)}")
                
            # 如果不是最后一个地址，继续尝试下一个
            if i < len(PLUGIN_API_URLS) - 1:
                logger.warning(f"正在尝试下一个插件API地址...")
        
        # 所有地址都失败
        logger.error(f"所有插件API地址均无法获取数据")
        self.plugins_data = {}

    def sort_plugins(self, plugins):
        """按插件在plugins_data中的原始索引（实际编号）排序"""
        # 获取plugins_data的键列表
        original_order = list(self.plugins_data.keys())
        # 按插件在原始列表中的索引位置排序
        return sorted(plugins.items(), key=lambda x: original_order.index(x[0]))

    # 渲染插件列表图片
    async def render_with_fallback(self, html_content, data={}):
        """尝试使用主渲染地址，如果失败则使用备用地址（优化日志输出）"""
        attempts = [
            (self.render_endpoint, "主渲染地址"),
            (self.fallback_render_endpoint, "备用渲染地址")
        ]
        
        for i, (endpoint, endpoint_name) in enumerate(attempts):
            try:
                # 输出当前尝试的地址（无论是否第一次）
                logger.info(f"开始渲染尝试 {i+1}/{len(attempts)}：使用{endpoint_name} {endpoint}")
                
                if i > 0:  # 第二次尝试（备用地址）时，明确输出切换日志
                    logger.warning(f"主渲染地址失败，已切换到{endpoint_name}：{endpoint}")
                
                # 切换当前渲染地址（即使是第一次尝试，也显式设置，避免地址残留）
                self.renderer.set_network_endpoint(endpoint)
                return await self.renderer.render_custom_template(html_content, data)
                
            except Exception as e:
                # 明确输出当前尝试失败的详细信息
                logger.error(f"渲染尝试 {i+1}（{endpoint_name}）失败: {str(e)}")
                # 若不是最后一次尝试，继续循环（进入下一个地址）
                if i < len(attempts) - 1:
                    continue
                # 最后一次尝试失败，抛出异常
                raise RuntimeError(f"所有渲染地址（共{len(attempts)}个）均失败")

        raise RuntimeError("未执行任何渲染尝试")

    # 渲染插件列表图片
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
        """渲染插件列表图片（共用逻辑）"""
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
            # 使用带备用地址的渲染方法
            img_url = await self.render_with_fallback(html_content, {})
            return img_url
        except Exception as e:
            logger.error(f"模板渲染失败: {str(e)}")
            raise

    @filter.command("插件市场")
    async def show_plugin_market(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        args = event.message_str.strip().split()
        page = 1
        if len(args) > 1 and args[1].isdigit():
            page = int(args[1])

        total_plugins = len(self.plugins_data)
        if total_plugins == 0:
            try:
                img_url = await self.render_plugin_list_image(
                    plugins=[],
                    total_items=0,
                    page=1,
                    total_pages=0,
                    title="✨ AstrBot插件市场",
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

        # 使用插件在原始列表中的索引作为编号
        plugin_items = [
            {
                "index": list(self.plugins_data.keys()).index(plugin_key) + 1,
                "key": plugin_key,
                "author": str(plugin_info.get("author", "未标注作者")),
                "desc": str(plugin_info.get("desc", "无描述信息")),
                "stars": plugin_info.get("stars", 0),
                "updated_at": self._format_time(plugin_info.get("updated_at", "")),
            }
            for i, (plugin_key, plugin_info) in enumerate(current_plugins)
        ]

        try:
            img_url = await self.render_plugin_list_image(
                plugins=plugin_items,
                total_items=total_plugins,
                page=page,
                total_pages=total_pages,
                title=f"✨ AstrBot插件市场 (第{page}/{total_pages}页)",
                next_page_command=f"/插件市场 {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"插件市场图片生成失败: {str(e)}")
            yield event.plain_result(
                "图片生成失败，以下是插件列表：\n"
                + f"当前第{page}/{total_pages}页，共{total_plugins}个插件"
            )

    @filter.command("插件搜索")
    async def search_plugins(self, event: AstrMessageEvent):
        await self.fetch_plugin_data()
        
        # 检查是否有搜索关键词
        if event.message_str is None:
            try:
                img_url = await self.render_plugin_list_image(
                    plugins=[],
                    total_items=0,
                    page=1,
                    total_pages=0,
                    title="🔍 插件搜索结果",
                    is_search=True,
                    search_term="",
                )
                yield event.image_result(img_url)
            except:
                yield event.plain_result("请输入搜索关键词（如：插件搜索 天气）")
            return

        input_str = event.message_str.strip()
        search_part = input_str[4:].strip() if len(input_str) >= 4 else ""
        if not search_part:
            try:
                img_url = await self.render_plugin_list_image(
                    plugins=[],
                    total_items=0,
                    page=1,
                    total_pages=0,
                    title="🔍 插件搜索结果",
                    is_search=True,
                    search_term="",
                )
                yield event.image_result(img_url)
            except:
                yield event.plain_result("请输入搜索关键词（如：插件搜索 天气）")
            return

        parts = search_part.split()
        page = 1
        if parts and parts[-1].isdigit():
            try:
                page = int(parts.pop())
                search_term = " ".join(parts)
            except ValueError:
                search_term = search_part
        else:
            search_term = search_part

        matched_plugins = self._filter_plugins_by_search_term(search_term)
        total_matches = len(matched_plugins)

        if total_matches == 0:
            try:
                img_url = await self.render_plugin_list_image(
                    plugins=[],
                    total_items=0,
                    page=1,
                    total_pages=0,
                    title="🔍 插件搜索结果",
                    is_search=True,
                    search_term=search_term,
                )
                yield event.image_result(img_url)
            except:
                yield event.plain_result(f"未找到包含 '{search_term}' 的插件")
            return

        total_pages = (total_matches + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))

        sorted_matches = sorted(matched_plugins.items(), key=lambda x: x[0])
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_matches = sorted_matches[start_idx:end_idx]

        # 使用插件在原始列表中的索引作为编号
        original_indices = [
            list(self.plugins_data.keys()).index(plugin_key) + 1
            for plugin_key, _ in sorted_matches
        ]
        plugin_items = [
            {
                "index": original_indices[start_idx + i],
                "key": plugin_key,
                "author": str(plugin_info.get("author", "未标注作者")),
                "desc": str(plugin_info.get("desc", "无描述信息")),
                "stars": plugin_info.get("stars", 0),
                "updated_at": self._format_time(plugin_info.get("updated_at", "")),
            }
            for i, (plugin_key, plugin_info) in enumerate(current_matches)
        ]

        try:
            img_url = await self.render_plugin_list_image(
                plugins=plugin_items,
                total_items=total_matches,
                page=page,
                total_pages=total_pages,
                title=f"🔍 插件搜索结果 (第{page}/{total_pages}页)",
                is_search=True,
                search_term=search_term,
                next_page_command=f"/插件搜索 {search_term} {page + 1}",
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"搜索结果图片生成失败: {str(e)}")
            yield event.plain_result(
                f"图片生成失败，搜索 '{search_term}' 共{total_matches}个结果"
            )

    def _filter_plugins_by_search_term(self, term: str) -> Dict[str, dict]:
        # 确保搜索词是字符串，避免None
        if term is None:
            return {}
        term_lower = term.lower()
        return {
            key: plugin
            for key, plugin in self.plugins_data.items()
            if term_lower in key.lower()
            # 强制转换为字符串，处理可能的None值
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
            yield event.plain_result("请指定要安装的插件编号、完整键名或GitHub仓库URL")
            return

        if self._is_github_repo_url(arg):
            yield event.plain_result("🔗 检测到GitHub仓库URL，准备从URL安装插件")
            async for result in self._install_plugin_from_url(arg, event):
                yield result
            return

        plugin_key = self._get_plugin_key_from_arg(arg)
        if not plugin_key:
            yield event.plain_result(f"未找到插件: {arg}")
            return

        plugin_info = self.plugins_data.get(plugin_key)
        if not plugin_info:
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

            await self.manage_plugin(
                plugin_key,
                plugin_info,
                self.plugins_dir,
                is_update=False,
                proxy=self.proxy,
            )

            await self.load_plugin(plugin_name)
            yield event.plain_result(f" 插件 {plugin_name} 安装并加载成功！")

        except Exception as e:
            logger.error(f"安装插件 {plugin_name} 失败: {e}", exc_info=True)
            yield event.plain_result(f" 安装失败: {str(e)}")

    def _get_plugin_key_from_arg(self, arg: str) -> Optional[str]:
        try:
            plugin_index = int(arg) - 1
            if 0 <= plugin_index < len(self.plugins_data):
                return list(self.plugins_data.keys())[plugin_index]
        except ValueError:
            pass
        return arg if arg in self.plugins_data else None

    def _is_github_repo_url(self, url: str) -> bool:
        return bool(GITHUB_REPO_REGEX.match(url))

    async def _install_plugin_from_url(self, repo_url: str, event: AstrMessageEvent):
        try:
            match = GITHUB_REPO_REGEX.match(repo_url)
            if not match:
                yield event.plain_result("无效的GitHub仓库URL格式")
                return

            author, repo_name = match.group(1), match.group(2)
            plugin_name = repo_name

            plugin_info = {
                "repo": repo_url,
                "name": plugin_name,
                "author": author,
                "desc": f"从URL安装的插件: {repo_url}",
            }

            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            yield event.plain_result(f"开始从URL安装插件: {plugin_name}")

            await self.manage_plugin(
                plugin_name,
                plugin_info,
                self.plugins_dir,
                is_update=False,
                proxy=self.proxy,
            )

            await self.load_plugin(plugin_name)
            yield event.plain_result(f" 插件 {plugin_name} 从URL安装并加载成功！")

        except Exception as e:
            logger.error(f"从URL安装插件失败: {e}", exc_info=True)
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
            raise ValueError(f"插件 {plugin_name} 未安装，无法更新")

        if is_update and backup_path.exists():
            shutil.rmtree(backup_path)
        if is_update:
            shutil.copytree(target_path, backup_path)

        try:
            logger.info(f"正在从 {repo_url} 下载插件 {plugin_name}...")
            await self.get_git_repo(repo_url, target_path, proxy)
            logger.info(f"插件 {plugin_name} 安装成功")
        except Exception as e:
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            if is_update and backup_path.exists():
                shutil.move(backup_path, target_path)
            raise RuntimeError(f"安装插件 {plugin_name} 时出错: {e}") from e

    async def get_git_repo(
        self, url: str, target_path: Path, proxy: Optional[str] = None
    ):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            repo_namespace = url.split("/")[-2:]
            author, repo = repo_namespace[0], repo_namespace[1]
            release_url = f"https://api.github.com/repos/{author}/{repo}/releases"

            try:
                response = await self.httpx_async_client.get(
                    release_url, follow_redirects=True
                )
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

            response = await self.httpx_async_client.get(
                download_url, follow_redirects=True
            )
            if response.status_code == 404 and "master.zip" in download_url:
                alt_url = download_url.replace("master.zip", "main.zip")
                logger.info("尝试下载main分支")
                response = await self.httpx_async_client.get(
                    alt_url, follow_redirects=True
                )
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

    async def load_plugin(self, plugin_name: str):
        try:
            await self.plugin_manager.load(specified_dir_name=plugin_name)
        except Exception as e:
            logger.warning(f"直接加载插件 {plugin_name} 失败: {e}，尝试重载")
            try:
                await self.plugin_manager.reload(specified_plugin_name=plugin_name)
            except Exception as reload_err:
                logger.error(f"重载插件 {plugin_name} 失败: {reload_err}")
                raise RuntimeError(f"加载插件失败: {e}, 重载尝试也失败: {reload_err}")

    async def terminate(self):
        await self.on_unload()

    @filter.command("插件排行")
    async def show_plugin_ranking(self, event: AstrMessageEvent):
        """插件排行指令：支持按时间或star数量排序"""
        await self.fetch_plugin_data()
        args = event.message_str.strip().split()

        sort_type = "star"
        if len(args) > 1:
            arg = args[1].lower()
            if arg in ["时间", "date", "updated"]:
                sort_type = "time"
            elif arg in ["star", "stars", "星级"]:
                sort_type = "star"

        # 解析页码
        page = 1
        if len(args) > 2 and args[2].isdigit():
            page = int(args[2])

        total_plugins = len(self.plugins_data)
        if total_plugins == 0:
            try:
                img_url = await self.render_plugin_list_image(
                    plugins=[],
                    total_items=0,
                    page=1,
                    total_pages=0,
                    title="插件排行榜",
                )
                yield event.image_result(img_url)
            except:
                yield event.plain_result("暂无插件数据")
            return

        # 根据排序类型排序插件
        sorted_plugins = self._sort_plugins_by_type(sort_type)

        # 分页处理
        total_pages = (total_plugins + self.page_size - 1) // self.page_size
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        current_plugins = sorted_plugins[start_idx:end_idx]

        # 使用插件在原始列表中的索引作为编号
        original_indices = [
            list(self.plugins_data.keys()).index(plugin_key) + 1
            for plugin_key, _ in sorted_plugins
        ]

        plugin_items = []
        for i, (plugin_key, plugin_info) in enumerate(current_plugins):
            plugin_items.append(
                {
                    "index": original_indices[start_idx + i],
                    "key": plugin_key,
                    "author": str(plugin_info.get("author", "未标注作者")),
                    "desc": str(plugin_info.get("desc", "无描述信息")),
                    "stars": plugin_info.get("stars", 0),
                    "updated_at": self._format_time(plugin_info.get("updated_at", "")),
                }
            )

        # 生成标题
        sort_text = "更新时间" if sort_type == "time" else "Star数量"
        title = f"插件排行榜（按{sort_text}排序）(第{page}/{total_pages}页)"

        try:
            # 渲染图片
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
                f"图片生成失败，当前是按{sort_text}排序的插件排行\n"
                + f"当前第{page}/{total_pages}页，共{total_plugins}个插件"
            )

    def _sort_plugins_by_type(self, sort_type: str) -> List[tuple]:
        """根据排序类型对插件进行排序"""
        if sort_type == "time":
            # 按更新时间排序
            return sorted(
                self.plugins_data.items(),
                key=lambda x: x[1].get("updated_at", ""),
                reverse=True,  # 最新的在前面
            )
        else:
            # 按star数量排序
            return sorted(
                self.plugins_data.items(),
                key=lambda x: x[1].get("stars", 0),
                reverse=True,  # 星星多的在前面
            )

    def _format_time(self, time_str: str) -> str:
        """格式化时间显示（将ISO时间转换为友好格式）"""
        if not time_str:
            return "未知时间"

        try:
            # 尝试ISO格式解析
            if "T" in time_str and "Z" in time_str:
                return time_str.replace("T", " ").split("Z")[0]

            # 尝试其他常见格式
            from datetime import datetime

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(time_str, fmt)
                    return dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass

            # 如果都失败，返回原始字符串
            return time_str
        except Exception as e:
            logger.warning(f"时间格式解析失败: {time_str}, 错误: {e}")
            return time_str    
