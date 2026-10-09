/* SPDX-License-Identifier: GPL-2.0-or-later
 * Pure bounded helpers: no system calls, allocation or external libraries.
 * Physical mapping and ownership/session checks stay in Python.
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned long uptr;
#define RECORD_BYTES 160000u
static void barrier(void) {
#if defined(__arm__)
    __asm__ __volatile__("dmb sy" ::: "memory");
#else
    __sync_synchronize();
#endif
}
int yt_native_version(void) { return 130; }
int yt_copy_slot(void *mapping, u32 slot, const void *source, u32 bytes) {
    if (!mapping || !source || slot>=8 || bytes!=RECORD_BYTES ||
        ((uptr)mapping&3u) || ((uptr)source&3u)) return -1;
    volatile u32 *dst = (volatile u32 *)((unsigned char *)mapping+0x1000u+slot*0x40000u);
    const u32 *src = (const u32 *)source;
    /* Volatile 32-bit accesses: never vectorize writes to the device mapping. */
    for (u32 i=0;i<RECORD_BYTES/4u;++i) dst[i]=src[i];
    barrier();
    u32 first=dst[0],last=dst[RECORD_BYTES/4u-1];
    barrier();
    return first==src[0] && last==src[RECORD_BYTES/4u-1] ? 0 : -2;
}
