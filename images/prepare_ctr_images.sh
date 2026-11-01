#!/bin/bash

cd "$(dirname "$0")"

# default: every instance dir holding a testbed.Dockerfile (args override)
IMAGELIST=${@:-$(echo */testbed.Dockerfile | xargs -n1 dirname)}

for image in ${IMAGELIST}
do
    echo "Building container image ${image}"
    podman build -t icr.io/vopo/swebench-${image}:latest -f ${image}/testbed.Dockerfile > /dev/null 2>&1
    if [[ $? != 0 ]]
    then
        echo "Build of ${image} failed. Stopping."
        exit 1
    fi
done
