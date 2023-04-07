#!/bin/bash
# 删除掉node依赖
sed -i '/CONFIG_NODEJS/d' .config