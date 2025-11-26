# Use Python 3.9
FROM python:3.9

# Set working directory to /code
WORKDIR /code

# Copy requirements and install dependencies
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Install ffmpeg manually (Crucial for Hugging Face Linux)
RUN apt-get update && apt-get install -y ffmpeg

# Copy the rest of the files
COPY . /code

# Create a cache directory that the user has permission to write to
# (This prevents "Permission Denied" errors with YouTube downloads)
RUN mkdir -p /tmp/cache
ENV XDG_CACHE_HOME=/tmp/cache
RUN chmod 777 /tmp/cache

# Run the app on port 7860 (Hugging Face's default port)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
```

**2. Update `server.py` (Small Port Tweak)**
Hugging Face expects your app to run on port **7860**.
Change the very last line of your `server.py` to this:

```python
if __name__ == "__main__":
    import uvicorn
    # Hugging Face expects port 7860
    uvicorn.run(app, host="0.0.0.0", port=7860)