# Firmware images

Drop AOS-CX `.swi` images in this directory and they appear in the Firmware
tab for selection.

This directory is a volume mount point — images are **not** stored in the
image or committed to git. Populate it however suits the host:

    # bind mount (docker-compose.yml)
    - ./firmware:/app/service/firmware:ro

    # or copy into the named volume
    docker cp ArubaOS-CX_6400-6300_10_16_0002.swi netforge-ui:/app/service/firmware/

Only files ending in `.swi` are listed.
