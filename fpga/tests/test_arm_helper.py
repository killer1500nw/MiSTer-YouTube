"""Execute the distributed ARM binary under Unicorn; requires pyelftools/unicorn."""
from pathlib import Path
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_LR,UC_ARM_REG_SP
binary=Path(__file__).resolve().parents[1]/'MiSTer-Scripts/YouTubeStreamSmooth/libyt_sender.so'
u=Uc(UC_ARCH_ARM,UC_MODE_ARM)
u.mem_map(0x100000,0x40000)
with binary.open('rb') as f:
    elf=ELFFile(f)
    for seg in elf.iter_segments():
        if seg['p_type']=='PT_LOAD':u.mem_write(0x100000+seg['p_vaddr'],seg.data())
    symbols={s.name:0x100000+s['st_value'] for s in elf.get_section_by_name('.dynsym').iter_symbols()}
base=0x30000000;source=0x10000000;size=0x201000;record=160000
u.mem_map(base,size);u.mem_map(source,0x30000)
u.mem_map(0x20000000,0x10000);u.reg_write(UC_ARM_REG_SP,0x20008000)
stop=0x130000
payload=bytes((i*17+i//1024)%256 for i in range(record));u.mem_write(source,payload)
writes=[]
def written(uc,access,address,size,value,data):
    if base<=address<base+0x201000:writes.append((address,size))
u.hook_add(UC_HOOK_MEM_WRITE,written)
def call(name,*args):
    for reg,value in zip((UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3),args):u.reg_write(reg,value)
    u.reg_write(UC_ARM_REG_LR,stop);u.emu_start(symbols[name],stop,count=2000000)
    return u.reg_read(UC_ARM_REG_R0)
assert call('yt_native_version')==130
for slot in range(8):
    u.mem_write(base,b'\xa5'*size);writes.clear()
    assert call('yt_copy_slot',base,slot,source,record)==0
    off=0x1000+slot*0x40000
    assert bytes(u.mem_read(base+off,record))==payload
    assert bytes(u.mem_read(base,off))==b'\xa5'*off
    assert bytes(u.mem_read(base+off+record,size-off-record))==b'\xa5'*(size-off-record)
    assert writes==[(base+off+i,4) for i in range(0,record,4)]
for args in ((base,8,source,record),(base,0,source,record-1),(0,0,source,record),(base,0,0,record),(base+1,0,source,record),(base,0,source+1,record)):
    writes.clear();assert call('yt_copy_slot',*args)==0xffffffff;assert not writes
print('PASS: distributed ARM helper version 130; all 8 slots; exact 32-bit writes; surrounding memory untouched; invalid arguments rejected.')
