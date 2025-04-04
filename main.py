from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.message_components import *
import asyncio
import sys
import importlib
from io import BytesIO
import time
import tempfile
import os
from google import genai  # 修改为正确的导入方式
from google.genai import types
from PIL import Image as PILImage
from google.genai.types import HttpOptions
from astrbot.core.utils.io import download_image_by_url



@register("astrbot_plugin_geminiexp", "YourName", "基于 Google Gemini 2.0 Flash Experimental 多模态模型的插件", "1.0.0")
class GeminiExpPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", "https://generativelanguage.googleapis.com")
        self.model = config.get("model", "gemini-2.0-flash-exp")
        self.waiting_users = {}  # 存储正在等待输入的用户 {user_id: {"expiry_time": time, "text_content": "", "image_list": []}}
        self.temp_dir = tempfile.mkdtemp(prefix="gemini_exp_")
        self.is_translating = config.get("translate", False)
        
        # 检查并安装必要的包
        if not self._check_packages():
            self._install_packages()
        
    def _check_packages(self) -> bool:
        """检查是否安装了需要的包"""
        try:
            importlib.import_module('google.genai')
            importlib.import_module('PIL')
            return True
        except ImportError:
            return False

    def _install_packages(self):
        """安装必要的包"""
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "google-genai", "pillow"])
            print("成功安装必要的包")
        except subprocess.CalledProcessError as e:
            print(f"安装包失败: {str(e)}")
            raise
        
    @filter.command("gemexp", alias=["edit", "ps"])
    async def gemini_exp(self, event: AstrMessageEvent):
        '''使用Gemini 2.0 Flash Experimental模型进行多模态交互'''
        # 检查API密钥是否配置
        if not self.api_key:
            yield event.plain_result("请联系管理员配置Gemini API密钥")
            return
        
        # 获取用户ID
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
                
        # 设置等待状态，有效期60秒
        self.waiting_users[user_id] = {
            "expiry_time": time.time() + 60,
            "text_content": "",
            "image_list": []
        }
        
        # 发送提示消息，然后返回，而不是继续处理
        yield event.plain_result(f"好的 {user_name}，请在60秒内发送您的文本描述和图片（如有）")
        return
    
    async def extract_image_from_component(self, component, user_data):
        """从消息组件中提取图片
        
        Args:
            component: 消息组件
            user_data: 用户数据字典，包含图片列表
        
        Returns:
            bool: 是否成功提取图片
        """
        if not isinstance(component, Image):
            return False
            
        try:
            # 获取图片URL
            img_url = None
            if hasattr(component, 'url') and component.url:
                img_url = component.url
            
            if img_url:
                # 使用框架提供的下载函数
                temp_img_path = await download_image_by_url(img_url)
                
                # 使用PIL打开图片
                img = PILImage.open(temp_img_path)
                user_data["image_list"].append(img)
                logger.info(f"成功下载图片: {img_url}")
                return True
                
        except Exception as e:
            logger.error(f"处理图片时出错: {str(e)}")
            raise e
            
        return False
    
    async def extract_images_from_chain(self, message_chain, user_data):
        """从消息链中提取所有图片
        
        Args:
            message_chain: 消息链
            user_data: 用户数据字典，包含图片列表
            
        Returns:
            int: 提取的图片数量
        """
        count = 0
        for msg in message_chain:
            if isinstance(msg, Image):
                success = await self.extract_image_from_component(msg, user_data)
                if success:
                    count += 1
            elif isinstance(msg, Reply):
                # 处理回复消息
                reply_chain = msg.chain
                logger.info(f"检测到回复消息，发送者: {msg.sender_nickname}({msg.sender_id})")
                
                # 从回复消息中提取图片
                reply_count = await self.extract_images_from_chain(reply_chain, user_data)
                count += reply_count
                
        return count
    
    @filter.event_message_type(EventMessageType.ALL)
    async def handle_follow_up(self, event: AstrMessageEvent):
        """处理用户的后续消息"""
        
        # 1. 快速初步检查 - 过滤明显不需要处理的消息
        if not isinstance(event, AstrMessageEvent):
            logger.error(f"handle_follow_up收到了错误类型的参数: {type(event)}")
            return
        
        user_id = event.get_sender_id()
        message_text = event.message_str.strip()
        
        # 忽略命令消息和触发词
        if message_text.startswith("/") or message_text.lower() in ["gemexp", "edit", "ps"]:
            logger.debug(f"忽略命令消息或触发词: {message_text}")
            return
        
        # 2. 会话状态检查
        if user_id not in self.waiting_users:
            logger.debug(f"用户 {user_id} 不在等待状态")
            return
        
        user_data = self.waiting_users[user_id]
        if time.time() > user_data["expiry_time"]:
            del self.waiting_users[user_id]
            yield event.plain_result("等待超时，请重新发送命令。")
            return
                
        # 3. 提取用户输入 - 先检查文本内容（快速操作）
        if user_data["text_content"] == "":
            cleaned_text = message_text.replace("gemexp", "").strip()
            cleaned_text = cleaned_text.replace("edit", "").strip()
            cleaned_text = cleaned_text.replace("ps", "").strip()
            user_data["text_content"] = cleaned_text
        
        # 4. 提取图片 - 只有文本内容存在时才处理图片
        message_chain = event.get_messages()
        try:
            image_count = await self.extract_images_from_chain(message_chain, user_data)
            logger.info(f"从消息中提取到 {image_count} 张图片")
        except Exception as e:
            logger.error(f"提取图片时出错: {str(e)}")
            yield event.plain_result(f"无法处理图片，请稍后再试或尝试其他图片。错误: {str(e)}")
            return
        
        # 5. 检查是否有文本 - 如果没有文本，提示用户
        if not user_data["text_content"]:
            logger.info(f"用户 {user_id} 没有提供任何文本")
            yield event.plain_result("请描述想要编辑的内容。")
            return
        # 6. 检查是否有图片 - 如果没有图片，提示用户
        if not user_data["image_list"]:
            logger.info(f"用户 {user_id} 没有提供任何图片")
            yield event.plain_result("请提供想要编辑的图片。")
            return
        
        # 7. 准备处理数据
        text_content = user_data["text_content"]
        image_list = user_data["image_list"]
        
        # 8. 移除用户等待状态并处理请求
        del self.waiting_users[user_id]
        
        # 9. 发送处理中消息并调用API
        logger.info(f"开始处理用户 {user_id} 的请求，文本长度: {len(text_content)}，图片数量: {len(image_list)}")

        # 9.5 调用LLM将text_content翻译为英语
        if self.is_translating:
            try:
                llm_response = await self.context.get_using_provider().text_chat(
                    prompt=text_content,
                    system_prompt="Translate the following text to English without additional explanation:",
                )
                text_content = llm_response.completion_text
            except Exception as e:
                logger.error(f"翻译文本时出错: {str(e)}")
                yield event.plain_result("翻译文本时出错，请稍后再试。")
                return

        yield event.plain_result(f"正在处理您的请求:{text_content} 请稍候...")
        
        # 10. 调用API和处理结果
        try:
            result = await self.process_with_gemini(text_content, image_list)
            text_response = result.get('text', '无文本回复')
            image_paths = result.get('image_paths', [])
            
            # 如果图片数量小于2，使用普通消息链发送
            if len(image_paths) < 2:
                # 构建回复消息链
                chain = [Plain(text_response)]
                
                # 添加图片到消息链
                for img_path in image_paths:
                    chain.append(Image.fromFileSystem(img_path))
                
                yield event.chain_result(chain)
            else:
                # 如果有2张或更多图片，使用群合并转发消息
                bot_id = self.config.get("bot_id", 114514)  # 使用配置或默认值
                bot_name = self.config.get("bot_name", "Gemini助手")  # 使用配置或默认值
                
                # 尝试将文本分割成与图片数量相等的部分
                text_parts = []
                
                # 尝试按段落分割文本（通过双换行符）
                paragraphs = text_response.split('\n\n')
                if len(paragraphs) >= len(image_paths):
                    # 如果段落数量足够，将它们分组为与图片数量相同的部分
                    for i in range(len(image_paths)):
                        if i < len(image_paths) - 1:
                            # 前面的图片取对应段落
                            parts_per_section = len(paragraphs) // len(image_paths)
                            start_idx = i * parts_per_section
                            end_idx = (i + 1) * parts_per_section
                            section_text = '\n\n'.join(paragraphs[start_idx:end_idx])
                            text_parts.append(section_text)
                        else:
                            # 最后一张图片取剩余所有段落
                            section_text = '\n\n'.join(paragraphs[i * (len(paragraphs) // len(image_paths)):])
                            text_parts.append(section_text)
                else:
                    # 如果段落不够，简单平均分配文本
                    total_chars = len(text_response)
                    chars_per_image = total_chars // len(image_paths)
                    
                    for i in range(len(image_paths)):
                        if i < len(image_paths) - 1:
                            # 前面的图片每个分配等量字符
                            start_idx = i * chars_per_image
                            end_idx = (i + 1) * chars_per_image
                            # 尝试在句子边界分割
                            j = end_idx
                            while j < min(total_chars, end_idx + 50) and j < total_chars:
                                if text_response[j] in ['.', '?', '!', '。', '？', '！']:
                                    end_idx = j + 1
                                    break
                                j += 1
                            text_parts.append(text_response[start_idx:end_idx])
                        else:
                            # 最后一张图片取剩余所有文本
                            text_parts.append(text_response[i * chars_per_image:])
                
                # 确保文本部分与图片数量一致
                if len(text_parts) < len(image_paths):
                    # 如果文本部分不够，填充空字符串
                    text_parts.extend([''] * (len(image_paths) - len(text_parts)))
                elif len(text_parts) > len(image_paths):
                    # 如果文本部分太多，合并多余部分到最后一个
                    text_parts = text_parts[:len(image_paths)-1] + ['\n\n'.join(text_parts[len(image_paths)-1:])]
                
                # 创建Nodes对象
                ns = Nodes([])
                
                # 向Nodes添加Node对象
                for i, (text_part, img_path) in enumerate(zip(text_parts, image_paths)):
                    # 添加适当的前缀
                    prefix = f"图片 {i+1}/{len(image_paths)}\n\n" if i > 0 else ""
                    
                    # 创建消息链
                    chain = [
                        Plain(prefix + text_part),
                        Image.fromFileSystem(img_path)
                    ]
                    
                    # 创建Node并添加到Nodes中
                    ns.nodes.append(
                        Node(
                            uin=bot_id,
                            name=bot_name,
                            content=chain
                        )
                    )
                
                # 将Nodes对象包装在列表中发送
                yield event.chain_result([ns])
            
        except Exception as e:
            logger.error(f"Gemini API调用失败: {str(e)}")
            yield event.plain_result(f"处理失败: {str(e)}")

    
    async def process_with_gemini(self, text, images):
        """处理图片和文本，调用Gemini API"""
        try:
            # 配置自定义 base_url
            http_options = HttpOptions(
                base_url=self.base_url
            )

            # 初始化客户端
            client = genai.Client(
                api_key=self.api_key,
                http_options=http_options
            )
            
            # 准备内容
            contents = []
            if text:
                contents.append(text)
            
            # 添加图片
            for img in images:
                contents.append(img)
            
            # 将内容转换为请求格式
            if len(contents) == 2 and text and len(images) == 1:
                # 如果是单文本+单图片，按照参考代码的格式处理
                contents = (text, images[0])
            
            if not contents:
                raise ValueError("没有有效的内容可以发送给Gemini API")
            
            # 调用API
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=['Text', 'Image'])
            )
            
            # 添加错误处理和日志记录
            logger.debug(f"Gemini API响应: {response}")
            
            # 解析响应
            result = {'text': '', 'image_paths': []}
            
            # 添加空值检查
            if not response:
                raise ValueError("Gemini API返回了空响应")
                
            if not hasattr(response, 'candidates') or not response.candidates:
                raise ValueError("Gemini API返回的candidates为空")
                
            if not hasattr(response.candidates[0], 'content') or not response.candidates[0].content:
                raise ValueError("Gemini API返回的content为空")
                
            if not hasattr(response.candidates[0].content, 'parts') or not response.candidates[0].content.parts:
                raise ValueError("Gemini API返回的parts为空")
            
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'text') and part.text is not None:
                    result['text'] += part.text
                elif hasattr(part, 'inline_data') and part.inline_data is not None:
                    # 将图片数据保存为临时文件
                    img_data = part.inline_data.data
                    img = PILImage.open(BytesIO(img_data))
                    
                    # 创建临时文件路径
                    temp_file_path = os.path.join(self.temp_dir, f"gemini_result_{time.time()}.png")
                    
                    # 保存图片
                    img.save(temp_file_path, format="PNG")
                    
                    # 添加图片路径到结果
                    result['image_paths'].append(temp_file_path)
            
            return result
            
        except Exception as e:
            logger.error(f"Gemini API处理失败: {str(e)}")
            raise e

    async def terminate(self):
        '''插件被卸载/停用时调用'''
        self.waiting_users.clear()
        
        # 清理临时文件
        if os.path.exists(self.temp_dir):
            try:
                for file in os.listdir(self.temp_dir):
                    os.remove(os.path.join(self.temp_dir, file))
                os.rmdir(self.temp_dir)
            except Exception as e:
                logger.error(f"清理临时文件时出错: {str(e)}")
