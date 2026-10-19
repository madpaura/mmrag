FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart
WORKDIR /sim
COPY mock_atlassian.py mock_lightrag.py mock_llm.py ./
