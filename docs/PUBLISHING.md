# 图修修：发布与部署

生产仓库为 `CSlawyer1985/tuxiuxiu`，网站为 `https://img.chenshi.ai/`。Cloudflare Pages连接GitHub的`main`分支，空构建命令，静态输出目录为`site/`。

## 发布结构

仓库存放README、干净Skill、介绍站、授权样图、使用文档和构建校验代码。运行SQLite数据库、缓存、日志、研究材料、备份、旧交付和平台运行环境不进入仓库。版本ZIP单独作为附件保存，两个包文件名不同，并附`SHA256SUMS.txt`。

## 推荐落地

1. 将`repository/`作为仓库根。按授权新建或选择远端，并仅提交当前文件清单。
2. 创建对应版本的Release，上传普通ZIP、Apple芯片Python3.14离线ZIP及校验文件。上传后核对字节数与摘要，再公开Release。
3. 为介绍站生成最终`_redirects`，把`/downloads/`中的两个固定文件名分别指向对应Release附件URL；不能把两个同名ZIP覆盖为一个附件。
4. Cloudflare Pages只发布`site/`。这是纯静态站，无需云端Python或修片服务。不要把整个工作区或仓库根当站点输出目录。
5. 发布后检查两条真实下载、安装说明、HTTPS、移动端与样图，再登记线上验收。

离线ZIP约139 MB，超过Cloudflare Pages单资源25 MiB限制，因此使用独立版本附件；GitHub Release单附件上限见官方文档。来源：[Pages限制](https://developers.cloudflare.com/pages/platform/limits/)、[Release说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

`python3 scripts/configure_downloads.py --repository <owner/name>`可在明确远端后生成站点重定向。该命令只写本地配置，不推送或部署。

## 上传前检查

核对`PUBLICATION-MANIFEST.json`中源码、站点及附件摘要；确认没有生成数据库、临时图片、执行日志、密钥或本机路径。实际组件许可证随包保留。README与站点下载同读`downloads.json`，上传附件的名称必须一致。

## 更新本站

仅在用户授权后提交并推送干净源码；Pages按生产分支构建静态站。发布新版本时先上传并核验版本附件，再更新下载清单和对应重定向；核对线上真实下载后登记结果。`release-assets/`与执行证据留在仓库外。
