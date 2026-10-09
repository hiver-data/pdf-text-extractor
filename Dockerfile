FROM apify/actor-python:3.13
USER root
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-nld && rm -rf /var/lib/apt/lists/*
ENV OMP_THREAD_LIMIT=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
CMD ["python", "-m", "src"]
