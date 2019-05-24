#!/usr/bin/python3
# Copyright 2019 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        https://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import pytest
import re
import requests
import subprocess
import time
import os.path

from fabric import Connection
from fabric import Config
from paramiko import SSHException

from .helpers import put

@pytest.fixture(scope="class")
def setup_test_container(request, setup_test_container_props, mender_version):
    # This should be parametrized in the mother project.
    image = setup_test_container_props.image_name

    if setup_test_container_props.append_mender_version:
        image = "%s:%s" % (image, mender_version)

    output = subprocess.check_output("docker run --rm --network host -tid %s" % image, shell=True)
    global docker_container_id
    docker_container_id = output.decode("utf-8").split("\n")[0]
    setup_test_container_props.container_id = docker_container_id

    def finalizer():
        subprocess.check_output("docker stop {}".format(docker_container_id), shell=True)
    request.addfinalizer(finalizer)

    ready = wait_for_container_boot(docker_container_id)

    assert ready, "Image did not boot. Aborting"
    return setup_test_container_props

@pytest.fixture(scope="class")
def setup_tester_ssh_connection(setup_test_container):
    yield new_tester_ssh_connection(setup_test_container)

def new_tester_ssh_connection(setup_test_container):
    config_hide = Config()
    config_hide.run.hide = True
    with Connection(host="localhost",
                user=setup_test_container.user,
                port=setup_test_container.port,
                config=config_hide,
                connect_kwargs={
                    "key_filename": setup_test_container.key_filename,
                    "password": "",
                    "timeout": 60,
                    "banner_timeout": 60,
                    "auth_timeout": 60,
                } ) as conn:

        ready = _probe_ssh_connection(conn)

        assert ready, "SSH connection can not be established. Aborting"
        return conn

def wait_for_container_boot(docker_container_id):
    assert docker_container_id is not None
    ready = False
    timeout = time.time() + 60*3
    while not ready and time.time() < timeout:
        time.sleep(5)
        output = subprocess.check_output("docker logs {} 2>&1".format(docker_container_id), shell=True)

        # Check on the last 100 chars only, so that we can detect reboots
        if re.search("(Poky|GNU/Linux).* tty", output.decode("utf-8")[-100:], flags=re.MULTILINE):
            ready = True

    return ready

def _probe_ssh_connection(conn):
    ready = False
    timeout = time.time() + 60
    while not ready and time.time() < timeout:
        try:
            result = conn.run('true', hide=True)
            if result.exited == 0:
                ready = True

        except SSHException as e:
            if not (str(e).endswith("Connection reset by peer") or str(e).endswith("Error reading SSH protocol banner")):
                raise e
            time.sleep(5)

    return ready

@pytest.fixture(scope="class")
def setup_mender_configured(setup_test_container, setup_tester_ssh_connection, mender_version):
    if setup_tester_ssh_connection.run("test -x /usr/bin/mender", warn=True).exited == 0:
        # If mender is already present, do nothing.
        return

    url = ("https://d1b0l86ne08fsf.cloudfront.net/%s/dist-packages/debian/armhf/mender-client_%s-1_armhf.deb"
           % (mender_version, mender_version))
    filename = os.path.basename(url)
    c = requests.get(url, stream=True)
    with open(filename, "wb") as fd:
        fd.write(c.raw.read())

    try:
        put(setup_tester_ssh_connection, filename, key_filename=setup_test_container.key_filename)
        setup_tester_ssh_connection.sudo("dpkg -i %s" % filename)
    finally:
        os.remove(filename)

    output = setup_tester_ssh_connection.run("uname -m").stdout.strip()
    if output == "x86_64":
        device_type = "generic-x86_64"
    elif output.startswith("arm"):
        device_type = "generic-armv6"
    else:
        raise KeyError("%s is not a recognized machine type" % output)

    setup_tester_ssh_connection.sudo("mkdir -p /var/lib/mender")
    setup_tester_ssh_connection.run("echo device_type=%s | sudo tee /var/lib/mender/device_type" % device_type)
