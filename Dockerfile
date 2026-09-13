# Dockerfile for the Earthquake Monitor + Shaking Estimator
#
# This encapsulates the ENTIRE working setup discovered during this
# project's development -- Python 3.12 (required by openquake.hazardlib,
# which is incompatible with newer Python versions), GDAL installed via
# conda-forge (pip's GDAL build repeatedly failed with compiler/version
# mismatches; conda-forge's prebuilt binaries worked cleanly), and all
# other dependencies. Building this image once means never repeating
# that debugging process again, on this machine or any other.
#
# BUILD:
#   docker build -t earthquake-monitor .
#
# RUN (live monitor):
#   docker run -it --rm \
#       -v $(pwd)/shakemaps:/app/shakemaps \
#       -v $(pwd)/earthquake_history.db:/app/earthquake_history.db \
#       earthquake-monitor
#
# RUN (instant test map, no live monitoring):
#   docker run -it --rm \
#       -v $(pwd)/shakemaps:/app/shakemaps \
#       earthquake-monitor python earthquake_monitor_with_shaking.py --test-map
#
# The -v flags mount local folders/files into the container so generated
# shake maps and the history database persist on your actual machine,
# not just inside the disposable container.
#
# NOTE ON DESKTOP NOTIFICATIONS: macOS desktop notifications (osascript)
# do NOT work from inside a Docker container -- they require direct
# access to the host's notification system, which containers don't have.
# If you want desktop notifications, run the script directly on your
# host machine (see the conda setup in README.md) rather than in Docker.
# Webhook notifications (Slack/Discord) DO work fine in Docker, since
# they're just outbound HTTP requests.

FROM continuumio/miniconda3:latest

WORKDIR /app

# Install system-level shared libraries that some conda packages (fiona,
# numpy, scipy) link against but don't reliably find via conda's own copy
# when run through `conda run` in this base image -- confirmed necessary
# after hitting "libgomp.so.1: cannot open shared object file" at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Create the conda environment with Python 3.12 (required -- hazardlib
# is incompatible with newer Python versions) and openquake.engine via
# conda-forge specifically. This is the exact combination that avoided
# the GDAL build failures encountered when trying pip-based installs.
RUN conda create -n openquake_conda python=3.12 -y && \
    conda install -n openquake_conda -c conda-forge openquake.engine -y && \
    conda clean -afy

# Activate the conda environment for all subsequent RUN/CMD instructions
SHELL ["conda", "run", "-n", "openquake_conda", "/bin/bash", "-c"]

# Install remaining dependencies via conda-forge (NOT pip) to keep the
# whole environment resolved by a single solver. Mixing pip on top of
# conda here caused a real failure: pip pulled a newer NumPy as a
# contextily/geodatasets dependency, breaking ABI compatibility with the
# pandas build conda's openquake.engine had already pinned to
# ("A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x").
RUN conda install -n openquake_conda -c conda-forge -y \
    websocket-client \
    requests \
    contextily \
    geodatasets \
    flask \
    folium \
    && conda clean -afy

# Copy the actual project files into the image
COPY config.py .
COPY earthquake_monitor_with_shaking.py .
COPY global_earthquake_monitor.py .
COPY shakemap_tool.py .
COPY openquake_shaking_estimate.py .
COPY dashboard.py .

# Create output directories so volume mounts have somewhere to attach
RUN mkdir -p /app/shakemaps

# Dashboard runs on this port when started as an alternative command
# (see usage notes below) -- EXPOSE is documentation for humans/tools,
# Docker still requires -p at `docker run` time to actually publish it.
EXPOSE 5001

# Default command: run the live monitor. To run the dashboard instead
# (in a separate container, sharing the same mounted volumes), override
# both --entrypoint and the command, e.g.:
#   docker run -it --rm -p 5001:5001 \
#       -v $(pwd)/shakemaps:/app/shakemaps \
#       -v $(pwd)/earthquake_history.db:/app/earthquake_history.db \
#       --entrypoint conda earthquake-monitor \
#       run --no-capture-output -n openquake_conda python dashboard.py
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "openquake_conda", "python", "earthquake_monitor_with_shaking.py"]
