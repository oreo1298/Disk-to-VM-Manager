; A boot sector that calls the VGA BIOS the way Windows 95/98 does when it shuts
; down, to test the VGA BIOS Rehome gives Windows 9x VMs.
;
; Windows 98 crashed with "A fatal exception 0D has occurred at C000:00002007"
; when it turned itself off: SeaVGABIOS (QEMU's VGA BIOS) prints a debug note
; for video functions it does not implement, and once Windows has set a video
; mode itself, the VGA BIOS runs on the caller's stack. With little of that stack
; left, the C code's 32-bit stack arithmetic goes below zero (or past 64 KB) and
; its stack addresses leave the 64 KB segment: in V86 mode that is exception 0D.
; QEMU's emulation (TCG) does not check segment limits, so the stores land just
; outside the segment instead, where this program looks for them.
;
; First it sets text mode 3 the old way (INT 10h AX=0003h), as Windows does when
; it shuts down, and reports whether SeaVGABIOS turned on its "extra stack" (BDA
; 40:B9h bit 6): a 512-byte stack in the E-segment, upper memory that Windows 9x
; and EMM386 use for other things, so Windows 98 then hung at a black screen.
;
; Then it turns the VGA BIOS's extra stack off (as a mode set by Windows does), and
; calls INT 10h AX=1200h BL=36h ("video refresh control", a stub in SeaVGABIOS)
; with SS = 1000h and SP from 0400h down to 0040h. Each time it counts the bytes
; changed in the 256 bytes on either side of that stack segment (linear 0FF00h-
; 0FFFFh and 20000h-200FFh). It reports the total, and AL of the last call, on
; the screen and COM1:
;   VGASTUB OUT=00000 AL=12 XS=0
;
; Build: nasm -f bin -o vgastub.img vgastub.asm

        bits 16
        org 0x7C00

STACK_SEG equ 0x1000
PAST_SEG  equ 0x2000                    ; STACK_SEG * 16 + 64 KB
BELOW_SEG equ 0x0FF0                    ; STACK_SEG * 16 - 256

start:  cli
        xor ax, ax
        mov ds, ax
        mov ss, ax
        mov sp, 0x7C00
        sti
        mov ax, 0x0003                  ; text mode the old way, as at shutdown
        int 0x10
        mov ax, 0x40
        mov es, ax
        mov al, [es:0xB9]
        shr al, 6
        and al, 1
        add al, '0'
        mov [line + 27], al
        mov word [low_sp], 0x0400
.try:   mov ax, PAST_SEG                ; canaries on both sides of the stack segment
        call fill
        mov ax, BELOW_SEG
        call fill
        mov ax, 0x40                    ; BDA 40:B9 bit 6: SeaVGABIOS's extra stack
        mov es, ax
        and byte [es:0xB9], ~0x40
        cli
        mov [saved_sp], sp
        mov ax, STACK_SEG
        mov ss, ax
        mov sp, [low_sp]
        sti
        mov ax, 0x1200
        mov bl, 0x36
        int 0x10
        cli
        xor cx, cx
        mov ss, cx
        mov sp, [saved_sp]
        sti
        mov [result_al], al
        mov ax, PAST_SEG                ; count the bytes the VGA BIOS wrote there
        call count
        mov ax, BELOW_SEG
        call count
        sub word [low_sp], 0x20
        cmp word [low_sp], 0x0040
        jae .try
        mov ax, [changed]
        mov di, line + 12
        call decimal
        mov al, [result_al]
        mov di, line + 21
        call hex2
        mov si, line
        call show
        cli
.halt:  hlt
        jmp .halt

fill:   mov es, ax                      ; 256 bytes of AAh at ax:0000
        xor di, di
        mov al, 0xAA
        mov cx, 256
        rep stosb
        ret

count:  mov es, ax                      ; [changed] += bytes at ax:0000 that are not AAh
        xor di, di
        mov cx, 256
.next:  cmp byte [es:di], 0xAA
        je .same
        inc word [changed]
.same:  inc di
        loop .next
        ret

decimal:                                ; ax -> 5 digits at ds:di (backwards from di+4)
        add di, 4
        mov bx, 10
        mov cx, 5
.digit: xor dx, dx
        div bx
        add dl, '0'
        mov [di], dl
        dec di
        loop .digit
        ret

hex2:   mov ah, al                      ; al -> 2 hex digits at ds:di
        shr al, 4
        call .nib
        mov al, ah
        and al, 0x0F
.nib:   add al, '0'
        cmp al, '9'
        jbe .ok
        add al, 'A' - '0' - 10
.ok:    mov [di], al
        inc di
        ret

show:   lodsb                           ; ds:si (0-terminated) on the screen and COM1
        or al, al
        jz .end
        mov ah, 0x0E
        xor bx, bx
        int 0x10
        mov dx, 0x3F8
        out dx, al
        jmp show
.end:   ret

saved_sp  dw 0
low_sp    dw 0
changed   dw 0
result_al db 0
line      db "VGASTUB OUT=00000 AL=00 XS=?", 13, 10, 0

        times 510 - ($ - $$) db 0
        dw 0xAA55
