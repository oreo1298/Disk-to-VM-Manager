; Rehome boot sector for Windows 95/98 and MS-DOS 7.x (IO.SYS) on FAT16 and FAT32.
;
; Copyright (C) 2026 Rehome contributors. GPL-3.0-or-later.
;
; Rehome writes this code into the boot sector of a partition that has no
; working boot code (for example one formatted with Linux's mkfs.fat), keeping
; the partition's own BPB. It does what grub4dos's "chainloader /io.sys" does,
; which is the long-proven way to start Windows 9x without Microsoft's boot
; sector: IO.SYS begins with a 2 KiB loader (MSLOAD) that expects the state
; Microsoft's boot sector leaves behind, so it is skipped, and the rest of
; IO.SYS is loaded to 0070:0000 and started with
;   DL = BIOS drive, DH = F8h (hard disk media byte), AX = BX = 0,
;   BP = 7C00h, DS = ES = SS = 0, SP = 0400h.
; Rehome checks that IO.SYS is an MS-DOS 7 one, at most 512 KiB, before
; writing this sector (Windows Me's compressed IO.SYS cannot be started this way).
;
; Disk reads use the BIOS extensions (INT 13h AH=42h), which every BIOS since
; the late 1990s and QEMU's SeaBIOS have. Sectors are 512 bytes.
;
; Build (scripts/gen_win9x_boot.py does this and stores the result):
;   nasm -f bin -o fat16.bin win9x_boot.asm
;   nasm -f bin -D FAT32 -o fat32.bin win9x_boot.asm

        bits 16
        cpu 386
        org 0x7C00

RELOC   equ 0x8000      ; segment the code moves to, out of IO.SYS's way (8000:7C00)
BUF     equ 0x7E00      ; one-sector buffer right after the code, same segment
LOADSEG equ 0x0070      ; IO.SYS without its loader goes to 0070:0000
SKIP    equ 4           ; sectors of MSLOAD at the start of IO.SYS (2 KiB)

; Variables just below the code (ss = ds = RELOC, bp = 7C00h); the stack is below them.
DATA    equ -4          ; dword: first sector of cluster 2, relative to the partition
DEST    equ -6          ; word: segment the next sector of IO.SYS goes to
SKIPPED equ -8          ; word: MSLOAD sectors still to skip, then negative
LEFT    equ -12         ; dword: sectors of IO.SYS still to load

%ifdef FAT32
DRIVE   equ 0x40        ; BPB offsets
CODE    equ 0x5A
%else
DRIVE   equ 0x24
CODE    equ 0x3E
%endif

start:  jmp short main
        nop
        times CODE - ($ - $$) db 0      ; the partition's own BPB goes here

main:   cli
        xor ax, ax
        mov ss, ax
        mov sp, 0x7C00
        mov ds, ax
        mov si, sp
        mov di, sp
        mov ax, RELOC
        mov es, ax
        mov cx, 256
        cld
        rep movsw
        mov ss, ax
        mov sp, 0x7C00 + LEFT
        mov ds, ax
        sti
        jmp RELOC:moved

moved:  mov bp, 0x7C00
        mov [bp+DRIVE], dl              ; the drive the BIOS booted from

%ifdef FAT32
        movzx eax, byte [bp+0x10]       ; FATs
        mul dword [bp+0x24]             ; x sectors per FAT
        movzx ecx, word [bp+0x0E]       ; + reserved sectors
        add eax, ecx
        mov [bp+DATA], eax
        mov eax, [bp+0x2C]              ; the root directory is a cluster chain
dir:    push eax
        call cluster
.sect:  call readbuf
        call scan
        inc eax
        loop .sect
        pop eax
        call next
        jnc dir
        jmp nofile
%else
        movzx eax, word [bp+0x16]       ; sectors per FAT
        movzx ecx, byte [bp+0x10]       ; x FATs
        mul ecx
        mov cx, [bp+0x0E]               ; + reserved sectors
        add eax, ecx                    ; = first sector of the root directory
        mov cx, [bp+0x11]               ; root entries, 16 per sector
        add cx, 15
        shr cx, 4
        push eax
        add eax, ecx
        mov [bp+DATA], eax
        pop eax
.sect:  call readbuf
        call scan
        inc eax
        loop .sect
        jmp nofile
%endif

; Look through the directory sector in BUF; jumps to found or nofile.
scan:   mov di, BUF
.entry: cmp byte [di], 0
        je nofile                       ; end of the directory
        pusha
        mov si, name
        mov cx, 11
        repe cmpsb
        popa
        je found
        add di, 32
        jns .entry                      ; up to BUF + 512 = 8000h
        ret

diskerr:
        mov si, msgdisk
        jmp short fail
nofile: mov si, msgfile
fail:   call print
        xor ax, ax
        int 0x16                        ; any key restarts
        int 0x19

found:
%ifdef FAT32
        push word [di+0x14]             ; first cluster, high word
%else
        push word 0
%endif
        push word [di+0x1A]
        pop eax
        mov ecx, [di+0x1C]              ; size in bytes -> sectors after MSLOAD
        dec ecx
        shr ecx, 9
        sub ecx, SKIP - 1
        jbe nofile
        mov [bp+LEFT], ecx
        mov word [bp+DEST], LOADSEG
        mov word [bp+SKIPPED], SKIP
file:   push eax
        call cluster
.sect:  dec word [bp+SKIPPED]
        jns .more                       ; MSLOAD is not loaded
        mov es, [bp+DEST]
        xor bx, bx
        call read
        add word [bp+DEST], 512 / 16
        dec dword [bp+LEFT]
        jz boot
.more:  inc eax
        loop .sect
        pop eax
        call next
        jnc file
        jmp nofile                      ; the cluster chain ends too early

boot:   mov dl, [bp+DRIVE]
        mov dh, 0xF8
        xor ax, ax
        xor bx, bx
        cli
        mov ss, ax
        mov sp, 0x400
        mov ds, ax
        mov es, ax
        sti                             ; bp is still 7C00h
        jmp LOADSEG:0

; eax = cluster -> eax = its first sector, cx = sectors per cluster
cluster:
        dec eax
        dec eax
        movzx ecx, byte [bp+0x0D]
        mul ecx
        add eax, [bp+DATA]
        ret

; eax = cluster -> eax = the next one in the FAT; carry set at the end of the chain
next:
%ifdef FAT32
        shl eax, 2
%else
        shl eax, 1
%endif
        mov di, ax
        and di, 511
        shr eax, 9
        movzx ecx, word [bp+0x0E]
        add eax, ecx
        call readbuf
%ifdef FAT32
        mov eax, [BUF+di]
        and eax, 0x0FFFFFFF
        cmp eax, 0x0FFFFFF8
%else
        movzx eax, word [BUF+di]
        cmp ax, 0xFFF8
%endif
        cmc
        ret

; Sector eax (relative to the partition) -> BUF
readbuf:
        push es
        push ds
        pop es
        mov bx, BUF
        call read
        pop es
        ret

; Sector eax (relative to the partition) -> es:bx
read:   pushad
        add eax, [bp+0x1C]              ; + hidden sectors = sector on the disk
        push dword 0
        push eax
        push es
        push bx
        push word 1
        push word 16
        mov si, sp
        mov ah, 0x42
        mov dl, [bp+DRIVE]
        int 0x13
        jc diskerr
        add sp, 16
        popad
        ret

print:  lodsb
        or al, al
        jz .done
        mov ah, 0x0E
        xor bx, bx
        int 0x10
        jmp short print
.done:  ret

name    db "IO      SYS"
msgfile db 13, 10, "Rehome: no IO.SYS", 0
msgdisk db 13, 10, "Rehome: disk error", 0

        times 510 - ($ - $$) db 0
        dw 0xAA55
