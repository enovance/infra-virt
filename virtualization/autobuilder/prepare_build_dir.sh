#!/bin/bash
# -*- coding: utf-8 -*-
#
# Copyright 2015 eNovance SAS <licensing@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
LOG_FILE="/srv/html/logs/$USER/build.txt"
[ -z $HOME ] && exit 1
[ -f ~/.ssh/id_rsa ] || ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
date -R > $LOG_FILE
pkill -P $(cat $HOME/build.pid)
[ -d $HOME/building ] && rm -r $HOME/building
cp -R $HOME/incoming $HOME/building

cd $HOME
[ -d config-tools ] || git init config-tools
cd config-tools
if [ ! -d .git/refs/remotes/goneri/ ]; then
    git remote add goneri git://github.com/goneri/config-tools.git
fi

git fetch --all
git checkout master
git branch -D goneri-wip
git reset --hard
git clean -ffdx
git checkout -b goneri-wip goneri/goneri-wip

cd $HOME

[ -d venv ] && rm -r venv
mkdir venv
virtualenv --system-site-packages venv # site-package: libvirt-python
source venv/bin/activate
pip install -rconfig-tools/virtualization/requirements.txt

cd $HOME/building
$HOME/config-tools/virtualize.sh localhost &> $LOG_FILE &

echo $! > $HOME/build.pid
