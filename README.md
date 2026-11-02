# ตัวแยก super.img สำหรับ Linux — v1.1.0

แยกอิมเมจพาร์ทิชัน **ทั้ง A และ B ที่มีข้อมูล** จาก metadata ทุก slot โดยอัตโนมัติ
เช่น system_a.img, system_b.img, vendor_a.img, vendor_b.img
ไม่ mount และไม่แตกไฟล์ภายใน system หรือพาร์ทิชันอื่น

## ใช้งานแบบสั้น

วาง unpack_super.py, install.sh และ super.img ในโฟลเดอร์เดียวกัน:

```bash
bash install.sh
python3 unpack_super.py -s super.img
```

ผลลัพธ์อยู่ที่ `./super_extracted/` ภายในโฟลเดอร์ที่รันคำสั่ง
ประกอบด้วยอิมเมจ `.img`, `SHA256SUMS.txt` และ `extraction_report.json`

ใช้ Python 3.8 ขึ้นไปและ standard library เท่านั้น ไม่ต้องติดตั้ง pip, lpunpack หรือ simg2img
การแตกไฟล์ไม่ต้องใช้ root ตัวติดตั้งใช้ sudo เฉพาะเมื่อต้องติดตั้งแพ็กเกจที่ขาด
ผ่าน apt-get, dnf, pacman, zypper หรือ apk

## พฤติกรรม A/B และการข้ามรายการว่าง

- มีข้อมูลทั้ง A และ B: แตกทั้งสองฝั่ง
- มีข้อมูลเพียง A หรือ B: แตกเฉพาะฝั่งที่มีข้อมูล
- รายการขนาด 0 ไบต์หรือไม่มี extents: ข้าม
- พาร์ทิชันมีขนาด แต่ข้อมูลทุกไบต์เป็นศูนย์: ข้าม
- ตรวจทั้งพาร์ทิชัน ไม่ได้ตัดสินจากข้อมูลช่วงต้นอย่างเดียว
- รายการซ้ำที่อ้างถึงช่วงข้อมูลเดียวกันใน metadata หรือสำเนาสำรอง: แตกเพียงครั้งเดียว
- ชื่อเดียวกันแต่อ้างถึงข้อมูลคนละช่วง: เก็บทุกชุด โดยเพิ่มชื่อเช่น
  `system_a.meta0-primary.img` และ `system_a.meta1-primary.img`
- บันทึกรายการที่ข้ามและแหล่ง metadata ของทุกไฟล์ไว้ในรายงาน

**metadata slot 0/1/2 กับชื่อพาร์ทิชัน _a/_b เป็นคนละอย่างกัน**
ตาราง metadata หนึ่งชุดอาจมีทั้ง A และ B และตารางหนึ่งอาจมีรายการว่าง
แต่ตารางอีกชุดยังอ้างถึงข้อมูลของชื่อเดียวกันอยู่ โปรแกรมจะเก็บรายการที่มีข้อมูลนั้น

ถ้าข้อมูลยังอ่านได้ แต่ header ระบบไฟล์ระบุขนาดไม่สอดคล้องกัน โปรแกรมยังแยก
อิมเมจตามช่วงข้อมูลที่ metadata ระบุ และแสดง `PASS WITH WARNINGS`
รายละเอียดอยู่ใน `filesystem_warnings` ของรายงาน การแยกสำเร็จไม่ได้หมายความว่า
ระบบไฟล์ภายในอิมเมจเก่าหรือข้อมูลที่เสียจะสมบูรณ์พร้อมใช้งาน

## ตัวเลือก

```bash
# ระบุปลายทางใหม่ รองรับช่องว่างในชื่อไฟล์
python3 unpack_super.py -s "/path with spaces/super.img" -o ./my_partitions

# แสดงตารางทั้งหมดโดยไม่สร้างผลลัพธ์
python3 unpack_super.py -s super.img --list

# ทางเลือก: จำกัดให้อ่าน metadata หมายเลข 1 เท่านั้น
# ปกติไม่ต้องระบุ --slot เพราะค่าเริ่มต้นอ่านทุกชุดเพื่อเก็บ A/B ให้ครบ
python3 unpack_super.py -s super.img --slot 1
```

ถ้าโฟลเดอร์ปลายทางมีอยู่แล้ว โปรแกรมจะหยุด ให้ใช้ `-o` ระบุโฟลเดอร์ใหม่
ไม่มีตัวเลือกสร้างไฟล์พาร์ทิชันว่างในเวอร์ชันนี้

## รูปแบบและการตรวจสอบ

- รองรับ super แบบ raw และ Android sparse v1 โดยอ่าน sparse ได้โดยตรง
- รองรับ liblp metadata v10.0–v10.2, linear/zero extents และ super ที่อยู่ในไฟล์เดียว
- ตรวจ SHA-256 ของ geometry, metadata header และตารางพาร์ทิชัน รวมทั้งสำเนาสำรอง
- ตรวจขอบเขตข้อมูลและใช้ header ของ EROFS/ext เพื่อรายงานความไม่สอดคล้อง
- ตรวจขนาดและ SHA-256 ของอิมเมจผลลัพธ์ทุกไฟล์เทียบกับข้อมูลที่อ่านจากต้นฉบับ
- ไม่เขียนทับปลายทางเดิม และเปลี่ยนชื่อโฟลเดอร์ชั่วคราวเป็นผลลัพธ์เมื่อสำเร็จครบ

ไม่รองรับ super ที่กระจายหลาย physical devices, OTA payload.bin หรือ snapshot/COW merge
ไม่ระบุ active boot slot และไม่ตรวจลายเซ็น AVB

## เครื่องมือตรวจระบบไฟล์เพิ่มเติม

ไม่จำเป็นสำหรับการแตก super แต่ติดตั้งเพิ่มได้ด้วย:

```bash
bash install.sh --check-tools
```

ติดตั้ง erofs-utils และ e2fsprogs เมื่อยังไม่มี ตัวอย่างการตรวจ EROFS:

```bash
fsck.erofs --extract super_extracted/system_b.img
```

`--extract` ที่ไม่มี `=โฟลเดอร์` ตรวจข้อมูลที่บีบอัดโดยไม่แตกลงโฟลเดอร์
ใช้กับ EROFS เท่านั้น ส่วน ext ใช้ `e2fsck -fn ชื่อ.img`
Python ไม่เรียกเครื่องมือตรวจระบบไฟล์เหล่านี้อัตโนมัติ

ตรวจ hash ซ้ำได้ด้วย:

```bash
cd super_extracted
sha256sum -c SHA256SUMS.txt
```

## ทดสอบ

```bash
python3 -B tests/test_unpack_super.py
```

ชุดทดสอบใช้ไฟล์จำลองขนาดเล็ก ไม่ต้องใช้ root
ดูผลทดสอบกับ super.img จริงใน TEST_REPORT.md และ validation/

อ้างอิงรูปแบบไฟล์จากต้นฉบับ AOSP:
[liblp metadata](https://android.googlesource.com/platform/system/core/+/refs/heads/main/fs_mgr/liblp/include/liblp/metadata_format.h),
[Android sparse](https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/sparse_format.h)
และ [ext superblock](https://www.kernel.org/doc/html/latest/filesystems/ext4/super.html)
