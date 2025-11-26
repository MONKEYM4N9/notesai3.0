FROM python:3.10

WORKDIR /code

# Install FFmpeg (Crucial for audio processing)
RUN apt-get update && apt-get install -y ffmpeg

# Install Python dependencies
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy your app code
COPY . .

# Create a cache directory (Fixes permission issues)
RUN mkdir -p /tmp/cache
ENV XDG_CACHE_HOME=/tmp/cache
RUN chmod 777 /tmp/cache

# Start the server (Render automatically sets the PORT variable)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]