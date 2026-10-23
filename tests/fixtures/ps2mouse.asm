; A boot sector that follows the PS/2 mouse like Windows 9x and 3.x do, for testing
; Rehome's relative mouse input (rehome/ui/console/relmouse.py).
;
; It reads mouse packets through the BIOS (INT 15h C2xx), applies Windows'
; default acceleration (a move of more than MouseThreshold1 = 6 counts on an
; axis is doubled, MouseSpeed = 1), keeps the cursor on a 640x480 screen and
; shows it on the first text line:  MOUSE X=00320 Y=00240 N=00000
; (N = packets received).
;
; Build: nasm -f bin -o ps2mouse.img ps2mouse.asm

        bits 16
        org 0x7C00

THRESHOLD equ 6

start:  cli
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov sp, 0x7C00
        sti
        mov ax, 0xC205                  ; initialise, 3-byte packets
        mov bh, 3
        int 0x15
        jc fail
        mov ax, 0xC207                  ; handler at ES:BX
        mov bx, handler
        int 0x15
        jc fail
        mov ax, 0xC200                  ; enable
        mov bh, 1
        int 0x15
        jc fail
main:   call show
        hlt
        jmp main

fail:   mov word [line], "NO"           ; "NOUSE": the BIOS has no PS/2 mouse services
        call show
        cli
        hlt

; Far call from the BIOS: [bp+12] status, [bp+10] X, [bp+8] Y, [bp+6] Z.
handler:
        push bp
        mov bp, sp
        pusha
        push ds
        xor ax, ax
        mov ds, ax
        mov ax, [bp+12]
        mov bx, [bp+10]
        xor bh, bh
        test al, 0x10                   ; X sign
        jz .x
        mov bh, 0xFF
.x:     mov cx, [bp+8]
        xor ch, ch
        test al, 0x20                   ; Y sign
        jz .y
        mov ch, 0xFF
.y:     neg cx                          ; PS/2 Y grows upwards, the screen's downwards
        mov ax, bx
        call accel
        mov si, posx
        mov dx, 639
        call move
        mov ax, cx
        call accel
        mov si, posy
        mov dx, 479
        call move
        inc word [packets]
        pop ds
        popa
        pop bp
        retf

accel:  mov di, ax                      ; ax = ax * 2 if |ax| > THRESHOLD
        or di, di
        jns .pos
        neg di
.pos:   cmp di, THRESHOLD
        jle .done
        shl ax, 1
.done:  ret

move:   add ax, [si]                    ; [si] = clamp([si] + ax, 0, dx)
        jns .low
        xor ax, ax
.low:   cmp ax, dx
        jle .ok
        mov ax, dx
.ok:    mov [si], ax
        ret

show:   push es
        mov ax, 0xB800
        mov es, ax
        mov ax, [posx]
        mov di, line + 8
        call decimal
        mov ax, [posy]
        mov di, line + 16
        call decimal
        mov ax, [packets]
        mov di, line + 24
        call decimal
        mov si, line
        xor di, di
        mov ah, 0x07
.char:  lodsb
        or al, al
        jz .end
        stosw
        jmp .char
.end:   pop es
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

posx    dw 320
posy    dw 240
packets dw 0
line    db "MOUSE X=00000 Y=00000 N=00000", 0

        times 510 - ($ - $$) db 0
        dw 0xAA55
