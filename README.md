# ตัวแยก super.img สำหรับ Linux

แยกเฉพาะอิมเมจพาร์ทิชัน เช่น `system_b.img`, `vendor_b.img`, `product_b.img`
ไม่ mount และไม่แตกไฟล์ภายใน system หรือพาร์ทิชันอื่น

## ใช้งานแบบสั้น

วาง `unpack_super.py`, `install.sh` และ `super.img` ในโฟลเดอร์เดียวกัน แล้วรัน:

```bash
bash install.sh
python3 unpack_super.py -s super.img
```

ผลลัพธ์อยู่ที่ `./super_extracted/` ภายในโฟลเดอร์ที่รันคำสั่ง ประกอบด้วย `.img`
ของพาร์ทิชัน, `SHA256SUMS.txt` และ `extraction_report.json`

ใช้ Python 3.8 ขึ้นไปและ standard library เท่านั้น ไม่ต้องใช้ pip, lpunpack หรือ simg2img
การแตกอิมเมจไม่ต้องใช้ root; ตัวติดตั้งขอ sudo เฉพาะเมื่อต้องติดตั้งแพ็กเกจที่ขาด
ผ่านตัวจัดการแพ็กเกจของระบบ (apt-get, dnf, pacman, zypper หรือ apk)

## ตัวเลือก

```bash
# ระบุปลายทางใหม่ และรองรับช่องว่างในชื่อไฟล์
python3 unpack_super.py -s "/path with spaces/super.img" -o ./my_partitions

# แสดงตารางทุก slot โดยไม่สร้างไฟล์ผลลัพธ์
python3 unpack_super.py -s super.img --list

# เลือก metadata slot เอง เมื่อหลายชุดใช้ได้และเลือกอัตโนมัติไม่ได้
python3 unpack_super.py -s super.img --slot 1

# รวมพาร์ทิชันว่างเป็นไฟล์ .img ขนาด 0 ไบต์
python3 unpack_super.py -s super.img --include-empty
```

ถ้าปลายทางมีอยู่แล้ว สคริปต์จะหยุด ให้ใช้ `-o` ระบุโฟลเดอร์ใหม่
ค่าเริ่มต้นจะข้ามพาร์ทิชันว่าง และบันทึกรายชื่อไว้ในรายงาน

## การตรวจสอบ

- รองรับ super แบบ raw และ Android sparse v1 โดยอ่าน sparse ได้โดยตรง
- ตรวจ SHA-256 ของ geometry, metadata header และตารางพาร์ทิชัน รวมทั้งสำเนาสำรอง
- ตรวจช่วงข้อมูลและขนาดระบบไฟล์ EROFS/ext ก่อนเลือก metadata
- ใช้ตารางที่สอดคล้องกับข้อมูลจริง หากยังมีหลายชุดที่เป็นไปได้จะให้เลือกด้วย `--slot`
- ตรวจขนาดและ SHA-256 ของอิมเมจผลลัพธ์ทุกไฟล์เทียบกับข้อมูลที่อ่านจากต้นฉบับ
- เขียนลงโฟลเดอร์ชั่วคราวก่อน แล้วเปลี่ยนชื่อเป็นปลายทางเมื่อสำเร็จครบ
- ปฏิเสธการเขียนทับปลายทางเดิม

รองรับ liblp metadata v10.0–v10.2, linear/zero extents และ super ที่อยู่ในไฟล์เดียว
ไม่รองรับชุด super ที่กระจายหลาย physical devices, OTA payload.bin หรือ snapshot/COW merge
การเลือก slot เป็นการตรวจความสอดคล้องของข้อมูล ไม่ใช่การระบุ active boot slot ของเครื่อง
การตรวจ SHA-256 ยืนยันการแยกข้อมูลตรงกับต้นฉบับ ไม่ใช่การตรวจลายเซ็น AVB

## เครื่องมือตรวจระบบไฟล์เพิ่มเติม (ไม่จำเป็นสำหรับการแตก super)

```bash
bash install.sh --check-tools
```

ตัวเลือกนี้ติดตั้ง `erofs-utils` และ `e2fsprogs` เพิ่มเมื่อยังไม่มี
ตรวจ EROFS โดยไม่เขียนไฟล์ภายในออกมาได้ดังนี้:

```bash
fsck.erofs --extract super_extracted/system_b.img
```

`--extract` ที่ **ไม่มี `=โฟลเดอร์`** จะตรวจข้อมูลที่บีบอัดโดยไม่แตกลงโฟลเดอร์
คำสั่งนี้ใช้กับอิมเมจ EROFS เท่านั้น ส่วน ext ใช้ `e2fsck -fn ชื่อ.img`
ตัวสคริปต์ Python ไม่เรียกเครื่องมือตรวจระบบไฟล์เหล่านี้อัตโนมัติ

ตรวจ hash ซ้ำได้ด้วย:

```bash
cd super_extracted
sha256sum -c SHA256SUMS.txt
```

## ทดสอบ

```bash
python3 -B tests/test_unpack_super.py
```

ชุดทดสอบใช้ไฟล์จำลองขนาดเล็กและไม่ต้องใช้ root
ผลการทดสอบกับ super.img จริงอยู่ใน `TEST_REPORT.md` และ `validation/`

อ้างอิงรูปแบบไฟล์จากต้นฉบับ AOSP:
[liblp metadata](https://android.googlesource.com/platform/system/core/+/refs/heads/main/fs_mgr/liblp/include/liblp/metadata_format.h),
[Android sparse](https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/sparse_format.h)
และ [ext superblock](https://www.kernel.org/doc/html/latest/filesystems/ext4/super.html)
