# 批量生成实战手册（管线选型 · 提示词经验 · 翻车模式速查）

来源：fanren-gal 项目 10 轮约 2500 张的脚本化生成，加上 comfyweb 批量生成功能上线后
的首轮实测（修仙女修 7 角色 × 7 张 = 49 张，角色套图模式，8 分半，零失败）。
功能用法见 README「批量生成」一节，本文只讲**怎么不出废图**。

## 1. 管线速查（内置预设，按内容类型选，不按个人喜好）

| 管线 | 模型组合 | 参数 | 适用 | 实测耗时 |
|---|---|---|---|---|
| anima | anima_turboV11 + qwen_3_06b_base + qwen_image_vae | 8步 CFG1 euler/simple | 人物立绘、图标、特效、有人的场景图 | 8~12s/张 |
| zimage | z-image-turbo-fp8 + qwen_3_4b + ae | 10步 CFG1 euler/simple | 纯背景、氛围图、**要正确渲染汉字**的牌匾/符纸 | 60~90s/张 |

- anima 只认英文标签（danbooru 风），混中文会被忽略；z-image 反而吃中文自然句。
- 尺寸：立绘 832×1216、场景/背景 1216×832、图标 832×832。
- 温度实测（RTX 3060 Ti）：anima 连续跑 46~65°C 基本不休息；z-image 53~61°C。
  引擎内置 ≥72°C 休 60s、≥78°C 休 180s，须与 ComfyUI 同机并设 `BATCHGEN_GPU_TEMP=1`。
- 换管线必须卸载显存（引擎组间自动 `POST /free`）；手动切换时也务必先 free，
  否则 turbo 模型 ↔ checkpoint 切换会触发 ComfyUI 显存清理 bug（IndexError）。

## 2. 提示词工程

1. **角色套图的一致性靠共享 base 词**：同一角色的立绘和全部场景图都拼同一段
   外貌+服装 base（refine 自动做到，提示词清单里也能看到）。这是零成本一致性手段；
   表情/服装差分再叠加同 seed（同 seed 只改描述词，脸不漂移）。
2. **场景描述会稀释人物特征**：anima 在 CFG1 下遵从性弱，base 在前、长场景描述在后，
  跑到后面发色/服装就自由发挥。对策见第 3 节 #1。
3. **锚点统一**：立绘 `standing, full body, white background, simple background, visual novel
   character sprite…`；场景 `in scene, detailed environment background, cinematic lighting…`；
   图标 `game item illustration, single object, floating…`（不要写 "game icon"——会被画成 UI 底座盒子）。
4. **模板用法**：正向模板做全局画风前缀（如 `chinese xianxia style`），负向模板做项目级
   排除（如修仙项目加 `crown, western`）。模板随「AI 细化」拼进每张图。
5. **LLM 细化产出必须先审后跑**：LLM 会自行解读设定（曾把"半面纱"写成
   `half veil covering eyes`，遮眼后角色媚感全失）。想要遮下半脸就写明
   `veil over lower face`。关键设定越具体，解读越不跑偏。

## 3. 翻车模式速查（案例 → 原因 → 对策）

| # | 症状 | 实测案例 | 原因 | 对策 |
|---|---|---|---|---|
| 1 | 场景图**发色/服装漂移** | 师娘场景图金发（设定黑盘发）；大师姐危机图银白长直发（设定黑高马尾） | CFG1/8步遵从弱，base 词被长场景描述稀释 | 场景描述**末尾重复发色+服装关键词**（refine 风格参考可加此规则）；或拆短场景描述；翻车单张换 seed 重roll |
| 2 | 立绘变**正背面设定图版式** | 师娘立绘出成 front+back turnaround，发色还偏了 | "standing, full body + white background" 锚点偶发诱导参考图画法 | 换 seed 重roll；提示词强调 `solo, one girl, single figure` |
| 3 | **伪文字/乱码** | 符咒上伪汉字、墙面装饰符文、角落 `@AI` 幽灵签名 | 负面 `text` 压不住装饰性纹样字 | 负面加 `letters, runes, signature, watermark`；尽量少让画面出现天然带字的物件；真要写字交给 zimage 给全文 |
| 4 | **西式元素渗入** | 场景背景出哥特花窗；女师尊仙袍画成西式礼服剪影 | 训练分布偏西方；描述里中式锚点不足 | 负面加 `western, cathedral, gothic`；描述里写明中式元素（飞檐、灯笼、水墨、汉服形制） |
| 5 | **LLM 解读偏差** | "半面纱"→遮眼 | LLM 直译设定词 | 清单先审后跑；设定写具体（见 2.5） |
| 6 | 道具**串味**（历史积累） | "游戏图标"画成 UI 底座盒子；修仙角色长帝王冠/十字架 | 词义诱导 + 训练数据噪声 | 正面 `item illustration, floating`；负面 `box, cube, crown, cross, headwear` |
| 7 | 压不住的别死磕 | "无人物"压不掉 z-image 画小人 | 模型先验太强 | 重试 2 次仍偏就改用途定位（如剪影图定位为事件 CG）或换提示词策略 |

经验法则：**翻车率健康线约 10%**（首轮实测 49 张里 5~6 张需重roll，符合）。
立绘全查，场景抽查 20%；翻车单张在任务清单里改 seed 重跑，不要整批重来。

## 4. 引擎与工程经验

- **同模型分组连跑、组间卸载显存**——交替提交每次多付 30~60s 模型重载，还可能踩
  显存 bug。引擎已内置（anima 全跑完再 zimage）。
- **LLM 大清单要长超时**：7 角色套图（49 任务）的 JSON 在推理模型上要跑几分钟，
  `_litegate_chat` 超时按 max_tokens 自适应（120~300s）；细分小请求不受影响。
- **温度节奏**：每张开画前查一次 `nvidia-smi`（引擎内置，需 `BATCHGEN_GPU_TEMP=1` 且
  与 ComfyUI 同机）。远程部署的 comfyweb 查不到 GPU 主机温度，保持默认关闭即可。
- **进度可视**：每张图落一条任务记录，进度/日志在页面实时可见，图片自动进画廊并带
  批次主题/画面类型元数据——出图后直接在画廊按批次筛选、点星标收下合格的。
- **数据管道纪律**（历史事故背书）：图库数据一律规则化重建，严禁解析/替换 HTML。

## 5. 标准流程清单

1. 写主题（角色套图模式附上已定稿角色的外貌英文，自由模式写清内容构成与数量）
2. AI 细化 → **逐条审清单**（角色设定是否被误读、场景弧线是否合理）
3. 选/存提示词模板 → 开始生成（预估时长：anima ≈ 10s/张，zimage ≈ 75s/张）
4. 跑完在画廊按批次筛选 → 立绘全查 + 场景抽查
5. 翻车的编辑任务换 seed 重roll；合格的重点星标收藏
