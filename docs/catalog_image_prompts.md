# 🖼️ برومبتات صور منتجات الكتالوج

الملف ده فيه **برومبت لكل نوع منتج** في سوق Mouss Tec (235 صورة: 188 طباعة وتصميم + 47 قطع غيار). الصورة الواحدة بتتحط على كل منتجات النوع ده في كل المتاجر (مثلاً صورة «بوجيهات إيريديوم» لكل الماركات).

كل صورة ليها **اسم ملف ثابت** — هو اللي بيربطها بالمنتجات لما تترفع، فلازم يفضل زي ما هو (المهم أول ٤ حروف: `p001` … `a047`).

## إزاي تستخدمه

1. افتح مولّد الصور اللي تحبه (ChatGPT أو Gemini أو غيره).
2. الصق **البرومبت الرئيسي** الأول، وبعده **دفعة واحدة** (١٠ صور) — المولّدات بتتعب لو بعتّ كله مرة واحدة.
3. اطلب منه الـ ZIP في الآخر ونزّله. كرّر لباقي الدفعات (كل دفعة في محادثة جديدة أحسن).
4. لو المولّد مابيعرفش يعمل ZIP: نزّل الصور واحدة واحدة **وسمّيها بنفس الاسم**.
5. ابعت الـ ZIPs، وهتترفع كلها مرة واحدة بالأمر:
   `python manage.py import_catalog_images الصور.zip`

> مش لازم تعمل كل الصور مرة واحدة — اللي ييجي يترفع، والباقي يفضل بالأيقونة لحد ما يكمل. وأي منتج إنت رافع له صورة بإيدك من الأدمن مابيتلمسش.

## البرومبت الرئيسي (الصقه الأول في كل محادثة)

```
I will give you a list of product photos to create for an online print-shop / car-parts catalog.
For EACH item in the list:
1. Generate ONE separate image, landscape 4:3 (1024x768 or larger), photorealistic product photography.
2. Follow the item's prompt exactly. Never put any text, letters, brand names, watermarks or logos in the image.
3. Save the image with EXACTLY the file name I give (keep the code at the start, e.g. p001-...png).
When you finish the whole list, put all the images in ONE ZIP file named like the batch (e.g. batch-01.zip) and give me the download link.
Do not skip any item. If you cannot finish all items, tell me exactly which file names are missing.
```

## دفعة 01 — طباعة وتصميم

```
Batch 01 (ZIP name: batch-01.zip):

File: p001-business-cards-350gsm-coated.png
Prompt: A neat fanned stack of thick white premium business cards with a minimal abstract navy geometric design, one card standing upright, top-down 3/4 angle. Product: Business cards, 350gsm coated. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p002-business-cards-with-spot-uv.png
Prompt: Matte black business cards with a glossy spot-UV abstract pattern catching the light, a few cards scattered at an angle. Product: Business cards with spot UV. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p003-embossed-business-cards.png
Prompt: Close-up of cream business cards with a raised embossed abstract emblem, raking side light showing the relief. Product: Embossed business cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p004-foil-business-cards-gold-silver.png
Prompt: Dark navy business cards with shiny gold foil abstract line art, slight reflections, elegant arrangement. Product: Foil business cards (gold/silver). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p005-clear-plastic-business-cards.png
Prompt: Transparent frosted plastic business cards with a simple colored abstract shape, light shining through them. Product: Clear plastic business cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p006-loyalty-discount-cards.png
Prompt: Colorful loyalty stamp cards with a row of empty circles, some stamped with a simple star, next to a rubber stamp. Product: Loyalty & discount cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p007-a5-flyers-double-sided.png
Prompt: A stack of glossy A5 promotional flyers with vibrant abstract color blocks, one flyer lifted showing the back side. Product: A5 flyers, double-sided. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p008-tri-fold-brochure.png
Prompt: An open tri-fold brochure standing upright showing three panels with abstract photos and color blocks. Product: Tri-fold brochure. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p009-laminated-restaurant-menu.png
Prompt: A thick laminated restaurant menu, folded open on a wooden cafe table, with abstract food-photo blocks and blank lines. Product: Laminated restaurant menu. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p010-delivery-menu-light-paper.png
Prompt: A stack of light paper delivery menus with colorful abstract food illustrations, slightly fanned. Product: Delivery menu (light paper). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 02 — طباعة وتصميم

```
Batch 02 (ZIP name: batch-02.zip):

File: p011-stapled-product-catalog.png
Prompt: A stapled product catalog booklet lying open, glossy pages with abstract product photo blocks, a closed copy beside it. Product: Stapled product catalog. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p012-a3-and-a2-posters.png
Prompt: Two large posters (A3 and A2) leaning against a wall with bold abstract colorful graphics. Product: A3 and A2 posters. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p013-spiral-notebook-with-your-logo-cover.png
Prompt: A spiral-bound notebook with a colorful printed abstract cover, next to a pen on a desk. Product: Spiral notebook with your logo cover. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p014-leather-planner-with-company-name.png
Prompt: A premium brown leather planner with an elastic strap and a subtle debossed abstract emblem on the cover. Product: Leather planner with company name. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p015-small-a6-gift-notebooks.png
Prompt: A small stack of A6 pocket notebooks in assorted pastel covers with simple abstract shapes. Product: Small A6 gift notebooks. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p016-wall-desk-calendars-with-your-design.png
Prompt: A wall calendar and a small desk tent calendar with colorful abstract photos, on a clean desk. Product: Wall & desk calendars with your design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p017-carbon-invoice-receipt-books.png
Prompt: Carbon-copy invoice and receipt books, pink white and yellow sheets visible at the edge, with a pen. Product: Carbon invoice & receipt books. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p018-kids-colouring-activity-books.png
Prompt: Kids coloring and activity books with cheerful abstract cartoon-style shapes, crayons around them. Product: Kids colouring & activity books. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p019-printed-white-ceramic-mug.png
Prompt: A glossy white ceramic coffee mug with a colorful abstract wraparound print, steam rising. Product: Printed white ceramic mug. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p020-magic-colour-changing-mug.png
Prompt: A black magic color-changing mug, half of it revealing a colorful abstract picture where hot coffee was poured. Product: Magic colour-changing mug. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 03 — طباعة وتصميم

```
Batch 03 (ZIP name: batch-03.zip):

File: p021-mug-with-coloured-handle-and-inside.png
Prompt: A white ceramic mug with a bright colored handle and colored interior, several color variants in a row. Product: Mug with coloured handle and inside. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p022-laser-engraved-stainless-tumbler.png
Prompt: A sleek stainless steel insulated tumbler with a laser-engraved minimal abstract emblem, condensation droplets. Product: Laser-engraved stainless tumbler. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p023-frosted-glass-mug.png
Prompt: A frosted glass mug with a soft printed abstract design, warm drink inside. Product: Frosted glass mug. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p024-gift-mug-set-in-a-box.png
Prompt: A gift box set of two printed ceramic mugs nested in a branded-looking box with tissue paper (no text). Product: Gift mug set in a box. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p025-plastic-promo-pens-with-logo.png
Prompt: A pile of colorful plastic promotional ballpoint pens in many colors. Product: Plastic promo pens with logo. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p026-laser-engraved-metal-pens.png
Prompt: Two heavy metal pens with laser-engraved minimal patterns on a dark slate surface. Product: Laser-engraved metal pens. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p027-pen-keychain-gift-set.png
Prompt: An open gift box containing a metal pen and a matching keychain on satin. Product: Pen & keychain gift set. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p028-whiteboard-markers-highlighters-with-your-name.png
Prompt: A row of whiteboard markers and highlighters in bright colors with blank clip areas. Product: Whiteboard markers & highlighters with your name. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p029-stylus-touch-pen.png
Prompt: A stylus touch pen resting on a smartphone screen. Product: Stylus touch pen. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p030-leather-acrylic-keychains.png
Prompt: An assortment of leather and acrylic keychains with simple abstract shapes. Product: Leather & acrylic keychains. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 04 — طباعة وتصميم

```
Batch 04 (ZIP name: batch-04.zip):

File: p031-branded-power-bank.png
Prompt: A slim power bank with a minimal abstract print, a cable plugged in, on a desk. Product: Branded power bank. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p032-usb-flash-drives-with-your-name.png
Prompt: Wooden and card-shaped USB flash drives laid out neatly. Product: USB flash drives with your name. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p033-award-shields-certificates.png
Prompt: An acrylic award shield and a wooden plaque with an engraved abstract emblem on a stand. Product: Award shields & certificates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p034-corporate-gift-box.png
Prompt: An open corporate gift box with a notebook, pen, mug and keychain arranged inside. Product: Corporate gift box. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p035-printed-mouse-pad.png
Prompt: A printed mouse pad with a colorful abstract design next to a computer mouse. Product: Printed mouse pad. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p036-embroidered-caps.png
Prompt: A few embroidered baseball caps in navy, black and white with a small abstract embroidered emblem. Product: Embroidered caps. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p037-printed-cotton-tote-bags.png
Prompt: Natural cotton tote bags hanging and folded, with a simple printed abstract shape. Product: Printed cotton tote bags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p038-laser-engraving-on-wood-leather.png
Prompt: Laser engraving on a wooden board and leather patch with a detailed abstract pattern, a laser beam glow. Product: Laser engraving on wood & leather. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p039-dtf-printed-cotton-t-shirts.png
Prompt: Folded and flat 100% cotton t-shirts in several colors with a bold abstract DTF print. Product: DTF printed cotton T-shirts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p040-restaurant-cafe-uniforms.png
Prompt: A restaurant staff uniform set: polo shirt, cap and apron neatly arranged, plain colors with a small abstract emblem. Product: Restaurant & cafe uniforms. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 05 — طباعة وتصميم

```
Batch 05 (ZIP name: batch-05.zip):

File: p041-corporate-polo-uniforms.png
Prompt: Corporate pique polo shirts in navy and white with a small embroidered abstract emblem on the chest. Product: Corporate polo uniforms. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p042-medical-scrubs-uniforms.png
Prompt: Medical scrubs in teal and navy, folded, with a small embroidered abstract emblem. Product: Medical scrubs & uniforms. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p043-sweatshirts-hoodies-with-your-design.png
Prompt: Heavy cotton hoodies and sweatshirts in grey and black with a large abstract print. Product: Sweatshirts & hoodies with your design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p044-school-nursery-uniforms.png
Prompt: A school uniform set: shirt, trousers and sweater with a small embroidered abstract crest. Product: School & nursery uniforms. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p045-event-marathon-t-shirts.png
Prompt: Light dri-fit event and marathon t-shirts in bright colors laid out in a row. Product: Event & marathon T-shirts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p046-printed-reflective-safety-vests.png
Prompt: High-visibility reflective safety vests in orange and yellow with reflective strips. Product: Printed reflective safety vests. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p047-roll-up-banner-stand.png
Prompt: A roll-up banner stand standing in an exhibition hall with bold abstract graphics. Product: Roll-up banner stand. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p048-flex-banner-for-shops.png
Prompt: A large vinyl flex banner with metal eyelets hanging on a shop front, abstract colorful design. Product: Flex banner for shops. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p049-illuminated-shop-sign.png
Prompt: An illuminated shop sign with glowing LED light box and raised 3D shapes at night. Product: Illuminated shop sign. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p050-vinyl-stickers-for-cars-storefronts.png
Prompt: A car partially wrapped with a colorful abstract vinyl design, and a glass storefront with frosted vinyl. Product: Vinyl stickers for cars & storefronts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 06 — طباعة وتصميم

```
Batch 06 (ZIP name: batch-06.zip):

File: p051-directional-signs-for-offices-clinics.png
Prompt: Acrylic office door signs and arrow wayfinding signs on a clean wall, abstract icons only. Product: Directional signs for offices & clinics. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p052-backdrop-for-conferences-photos.png
Prompt: A large backdrop wall with a repeated abstract pattern at a conference, spotlight lighting. Product: Backdrop for conferences & photos. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p053-die-cut-logo-stickers.png
Prompt: Die-cut vinyl stickers in fun abstract shapes, glossy, scattered on a laptop. Product: Die-cut logo stickers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p054-product-labels-on-a-roll.png
Prompt: A roll of printed product labels next to small jars with blank colorful labels applied. Product: Product labels (on a roll). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p055-kids-educational-stickers.png
Prompt: Sheets of colorful kids reward stickers with simple stars, hearts and animals. Product: Kids & educational stickers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p056-security-seal-stickers-hologram.png
Prompt: Holographic tamper-evident security seal stickers on a sealed cardboard box, rainbow shimmer. Product: Security seal stickers (hologram). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p057-printed-cardboard-product-boxes.png
Prompt: Printed cardboard product boxes in vibrant colors with abstract graphics, stacked. Product: Printed cardboard product boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p058-kraft-paper-bags-with-logo.png
Prompt: Kraft paper shopping bags with twisted handles and a simple abstract print. Product: Kraft paper bags with logo. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p059-printed-pizza-food-boxes.png
Prompt: Printed pizza boxes and burger boxes stacked with simple abstract graphics. Product: Printed pizza & food boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p060-rigid-magnetic-gift-boxes.png
Prompt: A rigid magnetic-closure gift box, half open, satin lining, matte finish. Product: Rigid magnetic gift boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 07 — طباعة وتصميم

```
Batch 07 (ZIP name: batch-07.zip):

File: p061-printed-plastic-bags.png
Prompt: Printed plastic shopping bags with colorful abstract graphics. Product: Printed plastic bags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p062-professional-logo-design.png
Prompt: A designer desk with a tablet showing several abstract logo concept sketches and color swatches. Product: Professional logo design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p063-full-brand-identity.png
Prompt: A brand identity mockup flat lay: business card, letterhead, envelope, and color palette swatches, abstract emblem. Product: Full brand identity. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p064-packaging-product-box-design.png
Prompt: Product packaging design concepts: boxes and labels in matching colors on a design desk. Product: Packaging & product box design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p065-restaurant-or-cafe-menu-design.png
Prompt: A menu design spread on a cafe table next to a coffee cup, abstract food blocks. Product: Restaurant or cafe menu design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p066-redraw-an-old-logo-as-vector.png
Prompt: A computer screen showing a crisp vector logo with bezier handles visible, abstract shape. Product: Redraw an old logo as vector. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p067-monthly-social-media-design-package.png
Prompt: A smartphone grid of colorful social media post designs (abstract), flat lay with plants. Product: Monthly social media design package. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p068-facebook-cover-profile-picture.png
Prompt: A laptop showing a social media profile page with a cover banner and profile picture (abstract). Product: Facebook cover & profile picture. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p069-story-reels-templates.png
Prompt: A smartphone showing vertical story templates with bold abstract graphics. Product: Story & reels templates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p070-sponsored-ad-design.png
Prompt: A smartphone showing an eye-catching sponsored ad design with abstract product imagery. Product: Sponsored ad design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 08 — طباعة وتصميم

```
Batch 08 (ZIP name: batch-08.zip):

File: p071-wedding-invitations.png
Prompt: Elegant wedding invitation cards with gold foil floral line art, envelopes and wax seal, on silk. Product: Wedding invitations. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p072-engagement-katb-ketab-aqiqah-invitations.png
Prompt: Engagement and celebration invitation cards with soft pastel floral patterns and ribbons. Product: Engagement, katb ketab & aqiqah invitations. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p073-event-giveaways-tags.png
Prompt: Wedding favor tags and small chocolate boxes with satin ribbon, pastel colors. Product: Event giveaways & tags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p074-conference-badges-lanyard-cards.png
Prompt: Conference name badges with lanyards on a table, blank name areas. Product: Conference badges & lanyard cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p075-letterhead-envelopes.png
Prompt: Letterhead paper and matching envelopes with a minimal abstract emblem on a desk. Product: Letterhead & envelopes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p076-clinic-prescriptions-appointment-cards.png
Prompt: A prescription pad and appointment cards on a doctor desk with a stethoscope. Product: Clinic prescriptions & appointment cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p077-pocket-folders.png
Prompt: Glossy presentation pocket folders with documents inside, abstract design. Product: Pocket folders. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p078-warranty-cards-product-certificates.png
Prompt: Numbered warranty cards and quality certificates with a guilloche border pattern (no readable text). Product: Warranty cards & product certificates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p079-self-inking-company-stamps.png
Prompt: A self-inking office stamp and an ink impression of an abstract round seal on paper. Product: Self-inking company stamps. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p080-kraft-paper-business-cards.png
Prompt: Natural brown kraft paper business cards with a minimal abstract stamp-like emblem, twine and dried leaves beside them. Product: Kraft paper business cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 09 — طباعة وتصميم

```
Batch 09 (ZIP name: batch-09.zip):

File: p081-thick-600gsm-cards-with-painted-edges.png
Prompt: A stack of thick luxury business cards showing brightly painted colored edges, close-up side view. Product: Thick 600gsm cards with painted edges. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p082-laser-die-cut-shaped-cards.png
Prompt: Die-cut business cards in unusual shapes (circle, leaf, rounded) scattered on a light surface. Product: Laser die-cut shaped cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p083-smart-nfc-tap-cards.png
Prompt: A matte black smart NFC card being tapped against a smartphone, soft glow between them. Product: Smart NFC tap cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p084-thank-you-cards-for-online-orders.png
Prompt: Small thank-you cards tucked into an open kraft shipping box with tissue paper. Product: Thank-you cards for online orders. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p085-gift-cards-vouchers.png
Prompt: Elegant gift voucher cards with abstract patterns, inside small envelopes tied with ribbon. Product: Gift cards & vouchers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p086-clothing-hang-tags.png
Prompt: Clothing hang tags with cotton string attached to neatly folded clothes. Product: Clothing hang tags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p087-salon-appointment-cards.png
Prompt: Pastel appointment cards next to a pen and a small plant on a salon counter. Product: Salon appointment cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p088-budget-handout-flyers.png
Prompt: A thick stack of thin colorful handout flyers with abstract color blocks, slightly fanned. Product: Budget handout flyers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p089-bi-fold-brochure.png
Prompt: An open bi-fold brochure standing on a desk showing abstract photo blocks and color panels. Product: Bi-fold brochure. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p090-company-profile-booklet.png
Prompt: A premium perfect-bound company profile booklet, one closed and one open showing abstract layouts. Product: Company profile booklet. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 10 — طباعة وتصميم

```
Batch 10 (ZIP name: batch-10.zip):

File: p091-door-hangers.png
Prompt: A colorful door hanger with abstract pattern hanging on a hotel room door handle. Product: Door hangers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p092-large-promo-posters.png
Prompt: A large bold abstract poster pasted on a city wall with a second poster rolled beside it. Product: Large promo posters. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p093-shelf-price-offer-tags.png
Prompt: Bright shelf talker price tags clipped to supermarket shelves with colorful products (no text). Product: Shelf price & offer tags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p094-numbered-discount-coupons.png
Prompt: A stack of perforated discount coupons with dashed tear lines and abstract color stripes. Product: Numbered discount coupons. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p095-dated-annual-planner-with-your-brand.png
Prompt: A thick dated annual planner with a ribbon bookmark and a hard cover with a subtle emblem, on an office desk. Product: Dated annual planner with your brand. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p096-notepads-sticky-notes.png
Prompt: Branded notepads and colorful sticky note pads arranged on a clean desk. Product: Notepads & sticky notes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p097-numbered-job-delivery-books.png
Prompt: Numbered carbonless delivery note books in white and yellow sheets, with a pen. Product: Numbered job & delivery books. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p098-desk-calendar-on-a-wooden-base.png
Prompt: A small desk calendar on a wooden base with abstract colorful monthly pages, beside a coffee cup. Product: Desk calendar on a wooden base. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p099-engraved-leather-cover-notebook.png
Prompt: An A5 notebook with a brown faux leather cover and a debossed abstract emblem, pen on top. Product: Engraved leather-cover notebook. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p100-tall-printed-latte-mug.png
Prompt: A tall white ceramic latte mug with a colorful abstract wraparound print, latte art inside. Product: Tall printed latte mug. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 11 — طباعة وتصميم

```
Batch 11 (ZIP name: batch-11.zip):

File: p101-mug-with-bamboo-lid-spoon.png
Prompt: A matte ceramic mug with a bamboo wooden lid and a small spoon, minimal abstract print. Product: Mug with bamboo lid & spoon. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p102-printed-sports-water-bottle.png
Prompt: Colorful aluminium sports water bottles with abstract printed bands, water droplets. Product: Printed sports water bottle. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p103-travel-mug-with-lid.png
Prompt: A sleek stainless travel mug with a sealed lid in a car cup holder, subtle engraved emblem. Product: Travel mug with lid. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p104-family-name-mugs.png
Prompt: A row of four pastel ceramic mugs in different colors on a kitchen shelf, simple abstract prints. Product: Family name mugs. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p105-eco-bamboo-pens.png
Prompt: Natural bamboo ballpoint pens with laser-engraved minimal emblem, on recycled paper. Product: Eco bamboo pens. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p106-luxury-pens-in-a-gift-box.png
Prompt: A luxury metal pen resting in an open velvet-lined gift box, dramatic soft light. Product: Luxury pens in a gift box. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p107-kids-name-pencils-colors.png
Prompt: A set of colorful wooden pencils and crayons in a pencil cup, bright and cheerful. Product: Kids name pencils & colors. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p108-stylus-pen-with-mini-torch.png
Prompt: A metallic multi-function pen with a stylus tip and a small glowing LED light. Product: Stylus pen with mini torch. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p109-printed-fridge-magnets.png
Prompt: Colorful printed fridge magnets with abstract shapes stuck on a white refrigerator door. Product: Printed fridge magnets. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p110-branded-wall-clocks.png
Prompt: A modern round wall clock with a minimal abstract printed face on a light wall. Product: Branded wall clocks. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 12 — طباعة وتصميم

```
Batch 12 (ZIP name: batch-12.zip):

File: p111-printed-umbrellas.png
Prompt: An open colorful umbrella with abstract printed panels, light rain in the background. Product: Printed umbrellas. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p112-branded-phone-laptop-stands.png
Prompt: A wooden phone stand and a clear acrylic stand holding smartphones on a desk. Product: Branded phone & laptop stands. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p113-printed-photo-cushions.png
Prompt: A soft velvet square cushion with a colorful abstract print on a sofa. Product: Printed photo cushions. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p114-custom-photo-puzzle.png
Prompt: A partially assembled jigsaw puzzle with a colorful abstract landscape picture, loose pieces around. Product: Custom photo puzzle. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p115-printed-acrylic-photo-keychains.png
Prompt: Clear acrylic keychains in various shapes with colorful abstract prints, on a key ring. Product: Printed acrylic photo keychains. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p116-button-pin-badges.png
Prompt: A pile of round colorful button pin badges with abstract patterns. Product: Button pin badges. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p117-branded-laptop-backpacks.png
Prompt: A modern dark grey laptop backpack with a small embroidered abstract emblem, studio shot. Product: Branded laptop backpacks. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p118-sports-trophies-medals.png
Prompt: Shiny gold sports trophies and medals with colorful ribbons on a podium. Product: Sports trophies & medals. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p119-embroidered-polo-shirts.png
Prompt: Folded navy cotton polo shirts with a small embroidered abstract emblem on the chest. Product: Embroidered polo shirts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p120-printed-aprons-chef-jackets.png
Prompt: A black canvas kitchen apron with a small emblem hanging next to a white chef jacket. Product: Printed aprons & chef jackets. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 13 — طباعة وتصميم

```
Batch 13 (ZIP name: batch-13.zip):

File: p121-matching-family-trip-t-shirts.png
Prompt: A row of matching colorful T-shirts in different sizes laid flat, playful abstract print. Product: Matching family & trip T-shirts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p122-embroidered-winter-jackets-blazers.png
Prompt: A dark padded winter jacket with a small embroidered emblem on a mannequin. Product: Embroidered winter jackets & blazers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p123-dri-fit-sports-t-shirts.png
Prompt: A vibrant sublimation-printed sports jersey with dynamic abstract shapes on a hanger. Product: Dri-fit sports T-shirts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p124-winter-beanies.png
Prompt: Knitted winter beanies in several colors with small embroidered patches. Product: Winter beanies. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p125-x-banner-stand.png
Prompt: An X-banner stand with a vibrant abstract graphic in a bright exhibition hall. Product: X-banner stand. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p126-feather-beach-flag.png
Prompt: Two tall feather beach flags with colorful abstract graphics waving outside a shop. Product: Feather beach flag. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p127-acrylic-metal-3d-letters.png
Prompt: Glowing backlit 3D abstract shapes made of brushed metal on a dark facade at night (no letters). Product: Acrylic & metal 3D letters. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p128-custom-led-neon-sign.png
Prompt: A glowing pink and blue LED neon abstract heart and wave shape on a brick wall. Product: Custom LED neon sign. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p129-floor-stair-graphics.png
Prompt: Colorful anti-slip floor graphic arrows on a shiny mall floor. Product: Floor & stair graphics. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p130-cardboard-display-stand.png
Prompt: A printed cardboard point-of-sale display stand with shelves holding small boxes, abstract graphics. Product: Cardboard display stand. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 14 — طباعة وتصميم

```
Batch 14 (ZIP name: batch-14.zip):

File: p131-acrylic-door-plates.png
Prompt: A frosted acrylic door sign with metal standoffs on a modern office door (no readable text). Product: Acrylic door plates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p132-clear-jar-container-labels.png
Prompt: Glass jars of honey and jam with clear printed labels showing abstract shapes. Product: Clear jar & container labels. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p133-laptop-phone-stickers.png
Prompt: A laptop lid covered with colorful die-cut vinyl stickers of abstract shapes. Product: Laptop & phone stickers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p134-sticker-sheets.png
Prompt: A colorful kiss-cut sticker sheet with many small abstract cute shapes, beside a planner. Product: Sticker sheets. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p135-packaging-seal-stickers.png
Prompt: A kraft paper bag sealed with a round glossy sticker with an abstract emblem. Product: Packaging seal stickers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p136-barcode-price-labels.png
Prompt: A roll of white barcode price labels next to a handheld label printer. Product: Barcode & price labels. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p137-frosted-glass-window-film.png
Prompt: A glass office partition with frosted film stripes and a subtle abstract pattern. Product: Frosted glass window film. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p138-printed-mailer-boxes.png
Prompt: An open printed corrugated mailer box with a colorful abstract interior print and tissue paper. Product: Printed mailer boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p139-sweets-chocolate-boxes.png
Prompt: An elegant pastry box with a clear window showing assorted chocolates and macarons. Product: Sweets & chocolate boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p140-perfume-cosmetics-boxes.png
Prompt: Small luxury perfume and cosmetic boxes with gold foil abstract lines, a perfume bottle beside them. Product: Perfume & cosmetics boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 15 — طباعة وتصميم

```
Batch 15 (ZIP name: batch-15.zip):

File: p141-printed-wrapping-tissue-paper.png
Prompt: Printed tissue paper with a repeating abstract pattern wrapped around a product in a box. Product: Printed wrapping & tissue paper. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p142-non-woven-bags.png
Prompt: A stack of colorful non-woven shopping bags with a simple printed abstract emblem. Product: Non-woven bags. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p143-printed-packing-tape.png
Prompt: Rolls of printed packing tape with a repeating abstract pattern on a sealed cardboard box. Product: Printed packing tape. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p144-stand-up-zipper-pouches.png
Prompt: Matte stand-up zipper pouches with abstract prints, coffee beans spilling in front. Product: Stand-up zipper pouches. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p145-rigid-gift-boxes-with-foam-insert.png
Prompt: A rigid black gift box with lid off showing a custom foam insert holding a pen and a small gadget. Product: Rigid gift boxes with foam insert. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p146-business-card-design.png
Prompt: Several elegant business card design mockups laid out on a designer desk with color swatches. Product: Business card design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p147-company-profile-design.png
Prompt: A designer laptop showing an abstract multi-page layout, printed spreads beside it. Product: Company profile design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p148-product-label-design.png
Prompt: Product bottles and jars with fresh abstract label designs, color swatches on the table. Product: Product label design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p149-shop-sign-facade-design.png
Prompt: A tablet showing an abstract storefront sign design mockup in front of a real shop facade. Product: Shop sign & facade design. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p150-ramadan-holiday-posts.png
Prompt: A smartphone showing a festive abstract crescent and lantern style post design, warm lights around. Product: Ramadan & holiday posts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 16 — طباعة وتصميم

```
Batch 16 (ZIP name: batch-16.zip):

File: p151-motion-graphics-short-reels.png
Prompt: A smartphone in vertical video mode showing dynamic abstract motion shapes, colorful light trails. Product: Motion graphics & short reels. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p152-instagram-highlight-covers.png
Prompt: A smartphone showing a row of minimal round highlight cover icons in pastel colors. Product: Instagram highlight covers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p153-baby-shower-newborn-invitations.png
Prompt: Pastel baby invitation cards with tiny cute abstract shapes, ribbons and small favor boxes. Product: Baby shower & newborn invitations. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p154-kids-birthday-invitations.png
Prompt: Colorful kids birthday party invitations with balloons and confetti abstract shapes, cake topper beside. Product: Kids birthday invitations. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p155-thank-you-greeting-cards.png
Prompt: Folded greeting cards with soft watercolor abstract florals and envelopes. Product: Thank-you & greeting cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p156-wedding-engagement-backdrops.png
Prompt: An elegant floral wedding photo backdrop with soft lights and a few chairs (no text). Product: Wedding & engagement backdrops. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p157-graduation-honour-certificates.png
Prompt: Elegant certificates with guilloche borders and a gold seal on premium cream paper. Product: Graduation & honour certificates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p158-wax-branding-iron-stamps.png
Prompt: A brass wax seal stamp with red wax seals on envelopes and a small branding iron. Product: Wax & branding iron stamps. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p159-pvc-employee-id-cards.png
Prompt: Blank-design plastic employee ID cards with lanyards and clear holders on a desk. Product: PVC employee ID cards. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p160-printed-envelopes-in-all-sizes.png
Prompt: A fanned set of envelopes in different sizes with a minimal abstract emblem. Product: Printed envelopes in all sizes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 17 — طباعة وتصميم

```
Batch 17 (ZIP name: batch-17.zip):

File: p161-receipt-payment-voucher-books.png
Prompt: Numbered voucher books with carbon copies, a calculator and a pen on an accountant desk. Product: Receipt & payment voucher books. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p162-patient-files-medical-report-forms.png
Prompt: Colored patient file folders and medical report forms on a clinic desk with a stethoscope. Product: Patient files & medical report forms. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p163-stretched-canvas-prints.png
Prompt: A large stretched canvas print with a colorful abstract landscape hanging above a sofa. Product: Stretched canvas prints. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p164-framed-photo-prints.png
Prompt: A gallery wall of black and natural wood frames with abstract photo prints. Product: Framed photo prints. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p165-acrylic-photo-prints.png
Prompt: A glossy acrylic photo panel with a vivid abstract seascape print, reflections on its surface. Product: Acrylic photo prints. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p166-multi-panel-wall-art-3-pieces.png
Prompt: A three-panel canvas wall art triptych with a flowing abstract design above a console table. Product: Multi-panel wall art (3 pieces). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p167-photo-prints-in-all-sizes.png
Prompt: A scattered stack of glossy photo prints with colorful abstract scenes, some instant-style. Product: Photo prints in all sizes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p168-printed-photo-book.png
Prompt: An open hardcover photo book with glossy pages showing abstract photo layouts. Product: Printed photo book. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p169-polaroid-style-prints-with-string-lights.png
Prompt: Polaroid-style photo prints clipped with wooden pegs on a warm LED fairy light string. Product: Polaroid-style prints with string lights. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p170-photo-prints-on-wood.png
Prompt: A photo printed on a natural wooden plank showing visible wood grain, on a shelf. Product: Photo prints on wood. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 18 — طباعة وتصميم

```
Batch 18 (ZIP name: batch-18.zip):

File: p171-school-exercise-books-with-school-logo.png
Prompt: A stack of school exercise books with colorful abstract covers and pencils on a classroom desk. Product: School exercise books with school logo. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p172-report-cards-school-certificates.png
Prompt: School report cards and certificates with decorative borders and a gold seal on a desk. Product: Report cards & school certificates. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p173-student-id-cards-with-photo.png
Prompt: Student plastic ID cards with lanyards in bright school colors. Product: Student ID cards with photo. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p174-name-labels-for-school-bags-lunch-boxes.png
Prompt: Small colorful waterproof name stickers on a kids lunch box and water bottle. Product: Name labels for school bags & lunch boxes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p175-printed-bound-study-notes.png
Prompt: A stack of spiral-bound printed study booklets with highlighters and sticky tabs. Product: Printed & bound study notes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p176-classroom-educational-charts.png
Prompt: Bright laminated educational wall charts with abstract shapes and colors in a cheerful classroom. Product: Classroom educational charts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p177-graduation-projects-theses.png
Prompt: A hardbound thesis with a dark leather cover and gold foil lines, next to a graduation cap. Product: Graduation projects & theses. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p178-school-event-backdrops.png
Prompt: A colorful graduation party backdrop with balloons and confetti abstract shapes on a school stage. Product: School event backdrops. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p179-printed-paper-coffee-cups.png
Prompt: Printed double-wall paper coffee cups with lids and abstract pattern sleeves on a cafe counter. Product: Printed paper coffee cups. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p180-printed-clear-plastic-cups.png
Prompt: Clear plastic cups of iced coffee and colorful juice with dome lids and a printed abstract emblem. Product: Printed clear plastic cups. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 19 — طباعة وتصميم

```
Batch 19 (ZIP name: batch-19.zip):

File: p181-paper-tray-mats.png
Prompt: A fast food tray with a printed paper tray mat with abstract food illustrations, burger and fries on it. Product: Paper tray mats. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p182-printed-paper-napkins.png
Prompt: A neat stack of white paper napkins with a small printed abstract emblem in a corner, beside a cup. Product: Printed paper napkins. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p183-cup-sleeves-carriers.png
Prompt: Kraft cup sleeves on coffee cups and a cardboard four-cup carrier. Product: Cup sleeves & carriers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p184-table-tents-with-offers.png
Prompt: A folded table tent card with abstract graphics and a QR-like pattern on a cafe table. Product: Table tents with offers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p185-printed-burger-sandwich-wrap.png
Prompt: A burger half-wrapped in printed greaseproof paper with a repeating abstract pattern. Product: Printed burger & sandwich wrap. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p186-acrylic-qr-menu-stands.png
Prompt: A small clear acrylic table stand with a QR-code-like pattern on a wooden cafe table. Product: Acrylic QR menu stands. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p187-take-away-paper-bags-with-handles.png
Prompt: Kraft take-away paper bags with twisted handles and a printed abstract emblem on a counter. Product: Take-away paper bags with handles. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

File: p188-printed-paper-food-containers.png
Prompt: Kraft paper food containers with clear lids holding colorful salads, abstract printed band. Product: Printed paper food containers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional e-commerce product photography, photorealistic, studio lighting, soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, premium print shop catalog, 4k

```

## دفعة 20 — قطع غيار

```
Batch 20 (ZIP name: batch-20.zip):

File: a001-iridium-spark-plugs.png
Prompt: A set of four iridium spark plugs with fine iridium tips, metallic and ceramic details. Product: Iridium spark plugs. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a002-standard-nickel-spark-plugs.png
Prompt: A set of four standard nickel spark plugs lined up. Product: Standard (nickel) spark plugs. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a003-ignition-coil.png
Prompt: A car ignition coil pack, black plastic and connectors. Product: Ignition coil. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a004-alternator.png
Prompt: A new car alternator with pulley, polished aluminium housing. Product: Alternator. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a005-starter-motor.png
Prompt: A new car starter motor with solenoid. Product: Starter motor. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a006-sensors-o2-crank-cam.png
Prompt: Car engine sensors: oxygen sensor, crankshaft and camshaft sensors laid out. Product: Sensors (O2 / crank / cam). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a007-front-brake-pads.png
Prompt: A pair of front ceramic brake pads. Product: Front brake pads. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a008-rear-brake-pads.png
Prompt: Rear brake pads and drum brake shoes. Product: Rear brake pads. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a009-brake-discs-rotors.png
Prompt: A pair of new ventilated brake discs (rotors). Product: Brake discs (rotors). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a010-dot4-brake-fluid.png
Prompt: A bottle of DOT4 brake fluid (plain unlabeled bottle). Product: DOT4 brake fluid. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

```

## دفعة 21 — قطع غيار

```
Batch 21 (ZIP name: batch-21.zip):

File: a011-front-shock-absorbers.png
Prompt: A pair of front shock absorbers / struts. Product: Front shock absorbers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a012-rear-shock-absorbers.png
Prompt: A pair of rear shock absorbers with mounts and bushes. Product: Rear shock absorbers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a013-control-arms-bushes.png
Prompt: A car suspension control arm with rubber bushes and ball joint. Product: Control arms & bushes. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a014-wheel-bearings.png
Prompt: A wheel bearing hub assembly. Product: Wheel bearings. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a015-stabiliser-links-anti-roll-bar.png
Prompt: Anti-roll bar stabiliser links and bushes. Product: Stabiliser links & anti-roll bar. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a016-oil-filter.png
Prompt: A car oil filter (plain, no brand). Product: Oil filter. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a017-air-filter.png
Prompt: A panel engine air filter. Product: Air filter. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a018-cabin-a-c-filter.png
Prompt: A carbon cabin air filter. Product: Cabin (A/C) filter. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a019-fuel-filter.png
Prompt: An inline fuel filter. Product: Fuel filter. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a020-full-synthetic-engine-oil.png
Prompt: A jug of full synthetic engine oil being poured, golden oil stream (unlabeled jug). Product: Full synthetic engine oil. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

```

## دفعة 22 — قطع غيار

```
Batch 22 (ZIP name: batch-22.zip):

File: a021-automatic-transmission-fluid.png
Prompt: A bottle of red automatic transmission fluid (unlabeled). Product: Automatic transmission fluid. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a022-tyres.png
Prompt: A stack of new car tyres with deep tread. Product: Car tyres. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a023-sport-rims.png
Prompt: A set of sporty alloy wheels (rims). Product: Sport rims. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a024-spare-wheel-tool-kit.png
Prompt: A spare wheel with a car jack and lug wrench. Product: Spare wheel & tool kit. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a025-battery.png
Prompt: A new car battery with terminals (plain, unbranded). Product: Battery. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a026-agm-battery-for-start-stop-cars.png
Prompt: An AGM start-stop car battery (plain, unbranded). Product: AGM battery for start-stop cars. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a027-radiator.png
Prompt: A new aluminium car radiator. Product: Radiator. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a028-water-pump.png
Prompt: A car water pump with gasket. Product: Water pump. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a029-a-c-compressor.png
Prompt: A car A/C compressor with pulley. Product: A/C compressor. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a030-radiator-fan-thermostat.png
Prompt: A radiator cooling fan with shroud and a thermostat. Product: Radiator fan & thermostat. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

```

## دفعة 23 — قطع غيار

```
Batch 23 (ZIP name: batch-23.zip):

File: a031-radiator-coolant.png
Prompt: A jug of green radiator coolant being poured (unlabeled). Product: Radiator coolant. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a032-timing-belt-kit.png
Prompt: A timing belt kit: toothed belt, idler and tensioner pulleys. Product: Timing belt kit. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a033-accessory-belt-alternator-a-c.png
Prompt: A ribbed serpentine accessory belt with tensioner. Product: Accessory belt (alternator/A/C). Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a034-timing-chain-kit.png
Prompt: A timing chain kit with gears and tensioner. Product: Timing chain kit. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a035-head-gasket-gasket-set.png
Prompt: A cylinder head gasket and full engine gasket set. Product: Head gasket & gasket set. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a036-engine-gearbox-mounts.png
Prompt: Engine and gearbox rubber mounts. Product: Engine & gearbox mounts. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a037-fuel-pump.png
Prompt: An in-tank electric fuel pump assembly with float. Product: Fuel pump. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a038-injectors.png
Prompt: A set of fuel injectors. Product: Injectors. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a039-headlight.png
Prompt: A modern car LED headlight unit. Product: Headlight. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a040-tail-light.png
Prompt: A pair of car tail light units, red. Product: Tail light. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

```

## دفعة 24 — قطع غيار

```
Batch 24 (ZIP name: batch-24.zip):

File: a041-led-xenon-bulbs.png
Prompt: Bright white LED headlight bulbs and xenon bulbs. Product: LED & xenon bulbs. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a042-fog-lights.png
Prompt: A pair of round car fog lights. Product: Fog lights. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a043-front-rear-bumpers.png
Prompt: A new unpainted car front bumper. Product: Front & rear bumpers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a044-side-mirrors.png
Prompt: A car side mirror with integrated indicator. Product: Side mirrors. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a045-wiper-blades.png
Prompt: A pair of car windshield wiper blades. Product: Wiper blades. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a046-floor-mats-seat-covers.png
Prompt: Custom 5D car floor mats and leather seat covers in a car interior. Product: Floor mats & seat covers. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

File: a047-android-screen-rear-camera.png
Prompt: An Android touchscreen car stereo head unit and a small rear-view camera. Product: Android screen & rear camera. Attractive composition, no readable text, no letters, no brand names and no logos anywhere in the image. professional automotive spare part product photography, photorealistic, studio lighting, clean white seamless background, sharp focus, high detail, new genuine part, 4k

```
