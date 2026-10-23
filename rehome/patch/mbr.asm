; Rehome master boot record: starts the boot sector of the active partition.
;
; Copyright (C) 2026 Rehome contributors. GPL-3.0-or-later.
;
; Rehome writes these 440 bytes over a master boot record that has no code,
; keeping the disk signature and partition table after them. Like the
; classic DOS MBR it moves itself to 0000:0600, loads the first sector of the
; active primary partition to 0000:7C00 and jumps there with DL = BIOS drive
; and DS:SI = BP = the partition's table entry. Reads use the BIOS
; extensions (INT 13h AH=42h).
;
; Build (scripts/gen_boot_code.py does this and stores the result):
;   nasm -f bin -o mbr.bin mbr.asm

        bits 16
        cpu 386
        org 0x600

start:  cli
        xor ax, ax
        mov ss, ax
        mov sp, 0x7C00
        mov ds, ax
        mov es, ax
        sti
        cld
        mov si, sp
        mov di, 0x600
        mov cx, 256
        rep movsw
        jmp 0:moved

moved:  mov bp, table
        mov cx, 4
.find:  test byte [bp], 0x80
        jnz found
        add bp, 16
        loop .find
        mov si, msgnone
        jmp short fail

found:  push dword 0
        push dword [bp+8]               ; first sector of the partition
        push ds
        push word 0x7C00
        push word 1
        push word 16
        mov si, sp
        mov ah, 0x42
        int 0x13
        mov si, msgdisk
        jc fail
        cmp word [0x7DFE], 0xAA55
        mov si, msgboot
        jne fail
        mov si, bp
        jmp 0:0x7C00

fail:   lodsb
        or al, al
        jz .halt
        mov ah, 0x0E
        xor bx, bx
        int 0x10
        jmp short fail
.halt:  xor ax, ax
        int 0x16                        ; any key restarts
        int 0x19

msgnone db 13, 10, "Rehome MBR: no active partition", 0
msgdisk db 13, 10, "Rehome MBR: disk read error", 0
msgboot db 13, 10, "Rehome MBR: the active partition has no boot code", 0

        times 440 - ($ - $$) db 0
table   equ 0x600 + 446
