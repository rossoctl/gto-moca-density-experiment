#!/bin/bash

export IMAGELIST=${@:-django__django-10914  django__django-11133  pydata__xarray-4094}

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
