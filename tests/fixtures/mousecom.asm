; MOUSE.COM: shows where the PS/2 mouse has moved a cursor, the way Windows 9x and 3.x
; move theirs, for testing the appliance's mouse path (tests/integration/test_appliance.py).
;
; Like ps2mouse.asm, but a DOS program: it reads mouse packets through the BIOS
; (INT 15h C2xx, so no DOS mouse driver may be loaded), doubles moves of more than
; 6 counts (Windows' MouseThreshold1), keeps the cursor on a 640x480 screen and
; shows it on the first text line until a key is pressed:
;   MOUSE X=00320 Y=00240 N=00000      (N = packets received)
;
; Build: nasm -f bin -o MOUSE.COM mousecom.asm

        bits 16
        cpu 186
        org 0x100

THRESHOLD equ 6

start:  mov ax, 0xC205                  ; initialise, 3-byte packets
        mov bh, 3
        int 0x15
        jc fail
        push cs
        pop es
        mov ax, 0xC207                  ; handler at ES:BX
        mov bx, handler
        int 0x15
        jc fail
        mov ax, 0xC200                  ; enable
        mov bh, 1
        int 0x15
        jc fail
main:   call show
        mov ah, 1                       ; a key quits
        int 0x16
        jnz quit
        sti
        hlt
        jmp main

quit:   xor ah, ah
        int 0x16
        mov ax, 0xC200                  ; disable, and no handler
        xor bh, bh
        int 0x15
        xor bx, bx
        mov es, bx
        mov ax, 0xC207
        int 0x15
        ret

fail:   mov word [line], "NO"           ; "NOUSE": the BIOS has no PS/2 mouse services
        call show
        ret

; Far call from the BIOS: [bp+12] status, [bp+10] X, [bp+8] Y, [bp+6] Z.
handler:
        push bp
        mov bp, sp
        pusha
        push ds
        push cs
        pop ds
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

decimal:                                ; ax -> 5 digits at ds:di
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
