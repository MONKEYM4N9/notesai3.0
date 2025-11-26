# Use Python 3.9
FROM python:3.9

# Set working directory
WORKDIR /code

# Copy requirements and install
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy the rest of the files
COPY . /code

# Create a folder for the cache to avoid permission errors
RUN mkdir -p /code/cache
os.environ['XDG_CACHE_HOME'] = '/code/cache'
RUN chmod 777 /code/cache

# Run the app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]