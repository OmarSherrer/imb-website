FROM python:3.12.3-slim-bookworm

ENV POETRY_VERSION 1.8.0

# Install system dependencies
RUN pip install "poetry==$POETRY_VERSION"

# Set work directory
WORKDIR /app

# Copy only requirements to cache them in docker layer
COPY poetry.lock pyproject.toml /app/

# Disable the creation of virtual environments
RUN poetry config virtualenvs.create false

# Install dependencies
RUN poetry install
# Ensure charset-normalizer uses pure Python
RUN python -m pip uninstall -y charset-normalizer && \
    python -m pip install --no-cache-dir --no-binary=charset-normalizer --no-build-isolation --force-reinstall charset-normalizer==3.4.2 && \
    find /usr/local/lib/python3.12/site-packages/charset_normalizer \
      -name '*.so' -delete && \
    CHARSET_NORMALIZER_USE_MYPYC=0 python -c \
      "import charset_normalizer; import requests; print('Dependencies OK')"
RUN playwright install --with-deps chromium

# Copy the current directory contents into the container at /app
COPY ./ /app/

# Fail the image build if the production application cannot be imported.
RUN python -c "from main import app; print('BACKEND_IMPORT_OK')"
