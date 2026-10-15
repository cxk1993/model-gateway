FROM python:3.11-slim
WORKDIR /app

# 数据文件从挂载卷读取（providers.json/config.json 等）
ENV GATEWAY_DATA_DIR=/app/data

COPY requirements.txt .
# 云端只需核心依赖（pywebview/pystray 是桌面客户端用的，不需要）
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx jinja2 pydantic Pillow

COPY app.py .
# 只拷面板模板本体（避免构建目录里的历史备份文件被打进镜像）
COPY templates/index.html ./templates/

EXPOSE 8000
# 容器内绑 0.0.0.0（Docker -p 127.0.0.1:8000:8000 保证仅宿主机 loopback 可达）
# umask 077：进程新建的数据文件默认仅属主可读写
CMD ["sh", "-c", "umask 077 && exec uvicorn app:app --host 0.0.0.0 --port 8000"]
