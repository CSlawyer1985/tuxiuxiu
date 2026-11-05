# 图修修：第三方组件说明

## 随Skill分发的模型

- Google MediaPipe Selfie Multiclass、Hair Segmenter、Face Landmarker：Apache-2.0。精确版本、来源、SHA-256与模型卡在`skill/assets/skin-model.json`；完整许可在`skill/assets/models/LICENSE-Apache-2.0.txt`，原始权重未修改。
- OpenCV Zoo YuNet：MIT；完整声明在`skill/assets/models/LICENSE-YuNet.txt`。

模型和依赖负责候选与像素处理，不做人脸身份识别。模型许可不代表照片质量保证。

## 平台运行库

锁定清单位于`skill/assets/runtime-requirements.txt`。离线包中的wheel保留各自的许可与NOTICE；运行环境不随源码仓库分发。

## 网站动效

GSAP及ScrollTrigger固定3.13.0，从官方npm发布取得并验证完整性；按随包GSAP标准许可使用。原始声明保留在`site/vendor/GSAP-LICENSE.txt`和库文件头，不改成MIT或Apache许可。官方安装与许可说明：[安装](https://gsap.com/docs/v3/Installation/)、[许可](https://gsap.com/community/standard-license/)。

## 样图

现有测试照片的提供者已明确确认可公开展示，原图署名与影像内容保留；新增培训图为生成测试素材，详情见DEMO-ASSETS.md。公开展示授权与软件许可分别说明。

## 自有代码

自有代码当前未指定统一的开源许可证。本说明只登记实际第三方组件，不替整个项目指定开放源代码许可。
