"""视图层:按功能域拆分的 Flask 蓝图,由 app.py 的 create_app() 逐个注册。

每个模块暴露 register(app);可选暴露 warm()(WS 连上 ComfyUI 后的缓存预热)。
模块导入/注册失败只跳过该功能,不影响其他模块(app.py 统一兜底记日志)。
"""
