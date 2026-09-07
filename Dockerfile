FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt update && apt install -y python3 python3-pip curl git

# libreoffice-calc computes the formula results of a submitted workbook.
# openpyxl has no formula engine and drops the value cache of any workbook it
# writes, so without it an edited spreadsheet grades as blank cells. Calc alone
# covers the xlsx conversion; the rest of the suite is left out of the image.
RUN apt install -y --no-install-recommends libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
RUN uv venv --python 3.11

# Judge served through an OpenAI-compatible gateway; a caller can override this
# and OPENAI_BASE_URL to grade with a different model or endpoint.
ENV JUDGE_MODEL=openai/gpt-5.6-luna

COPY . /app
RUN uv pip install -r /app/requirements.txt

EXPOSE 8080
CMD ["uv", "run", "python", "/app/server.py"]
