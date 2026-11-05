# 培训示例素材与处理说明

培训现场使用内置图像生成工具新建，未使用现有人物照片作参考。它用于展示暗光调色和水印流程，不是真实活动纪实，也不作为皮肤分割准确率样本。全景和局部来自同一张底图。

优化图由随包本地像素执行器完成提亮、层次、色彩与适度锐化，再把透明水印实际合入右下角；没有分别生成一张“更好”的照片冒充修图结果。原图和优化图人物、物件及几何关系保持。

实际配方：exposure_ev=1.8，shadows=0.9，highlights=-1.3，contrast=0.65，warmth=-6.5，tint=2.2，saturation=0.1，sharpen=0.95。培训素材没有开启磨皮。JPEG导出会发生重编码。人像来自已有原片及有效技术核对的可见皮肤蒙版；本轮展示磨皮强度0.85，曝光增量0.25EV，肤色提亮0.08EV，锐化0.3；输出带有右下角水印。上述为网站展示强度，不修改活动记录里的参数。

## 生成方式与完整提示词

方式：内置 image_gen 工具，单张新建；后续使用本地非生成式像素处理与水印合成。

```text
Use case: photorealistic-natural. Create ONE horizontal documentary photograph, 16:10 landscape, suitable as a deliberately imperfect BEFORE photo for a photo-retouching software demonstration. A plausible Chinese professional training workshop in a modest hotel training room, around 24 adult attendees in business casual clothes seated at rows of narrow conference tables with notebooks and paper cups; a trainer beside a projection screen at the front. View from a rear-side aisle, medium-wide eye-level camera, some attendee faces in side profile and a few foreground faces visible, coherent natural human anatomy, realistic ordinary atmosphere, no identifiable real people or public figures. Lighting intentionally underexposed about one stop: dim overhead lights, uneven warm illumination, slight muddy gray-green color cast, some natural high-ISO grain and modest phone-camera softness. Important: preserve visible recoverable detail in faces, clothes, walls and tables, no crushed pitch-black shadows, no heavy motion blur, no cinematic effects. The picture should look like a usable but poorly exposed event snapshot rather than polished advertising. Screen has a simple understated blurred training slide without legible brand or logo. No watermark, no branding, no frame, no comparison layout, no text overlay. This is only the dark original source; the AFTER will be made with deterministic local photo adjustments, so do not make a before/after pair.
```
