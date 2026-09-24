FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

COPY requirements.txt /tmp/project-requirements.txt

RUN python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        -r /tmp/project-requirements.txt \
    && rm /tmp/project-requirements.txt

# The base image's Yarn source has an invalid signing key and blocks feature APT updates.
RUN rm -f /etc/apt/sources.list.d/yarn.list
