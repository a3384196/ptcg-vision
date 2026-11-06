FROM python:3.12-slim
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY vision.py server.py board.py game.py ai.py ./
COPY data ./data
COPY static ./static
ENV PTCG_DB=/app/db
EXPOSE 8765
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
