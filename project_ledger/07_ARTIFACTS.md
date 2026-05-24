# Artifacts

## 代码入口

- `aiarchive/main.py`：FastAPI 应用入口。
- `aiarchive/services.py`：导入、检索、列表和数据持久化服务逻辑。
- `aiarchive/importers/`：各平台导出格式导入器。

## 构建命令

待确认。

## 运行命令

- `uvicorn aiarchive.main:app --host 0.0.0.0 --port 8000`

## 测试命令

待确认。

## 输出文件

- `data/archive.db`：SQLite 数据库，按配置路径推断。
- `data/media/`：导入附件和媒体文件目录，按配置路径推断。
- `imports/`：导入过程临时目录或工作目录，按配置路径推断。

## 重要文档

- `README.md`
- `README.zh-CN.md`
- `LICENSE`

## 重要配置

- `requirements.txt`
- `aiarchive/config.py`

## 部署方式

待确认。README 仅确认了本地 `uvicorn` 运行方式。

## 备注

- 项目说明强调只应导入用户有合法权利访问、保存和处理的数据。
- 不要公开发布 `data/`、数据库文件、导入附件或真实聊天导出内容，除非已确认合法权利。
