; A stand-in for an MS-DOS 7 (Windows 95/98) IO.SYS, to test Rehome's boot sector.
;
; Laid out like the real file: an "MZ" header and the 2 KiB loader MSLOAD
; ("BJ" at 200h, "MS" at 7FEh), then the body that a boot sector starting
; IO.SYS like grub4dos does must load to 0070:0000. The body reports the
; registers it was started with and whether every byte after it arrived in
; order, on the screen and on COM1:
;   IOSYS ax=0000 bx=0000 dx=F880 sp=0400 bp=7C00 ss=0000 ds=0000 es=0000 cs=0070 sum=OK
; Code that starts MSLOAD instead (at 0070:0200, the MS-DOS 7 way) gets
;   MSLOAD ss=.... bp=.... bps=....
; where bps is the word at SS:BP+0Bh (512 when SS:BP is the boot sector).
; tests/fixtures/windows.py appends the data and fills in the header.

        bits 16
        cpu 386

%macro OUTPUT 0                 ; print ds:si (0-terminated) on the screen and COM1
%%next: lodsb
        or al, al
        jz %%done
        mov ah, 0x0E
        xor bx, bx
        int 0x10
        mov dx, 0x3F8
        out dx, al
        jmp short %%next
%%done:
%endmacro

%macro HEX 0                    ; ax -> 4 hex digits at es:di
        mov cx, 4
%%digit:
        rol ax, 4
        push ax
        and al, 0x0F
        add al, '0'
        cmp al, '9'
        jbe %%ok
        add al, 'A' - '0' - 10
%%ok:   stosb
        pop ax
        loop %%digit
%endmacro

        section msload start=0 vstart=0
        db "MZ"
        times 0x200 - ($ - $$) db 0
        db "BJ"                         ; inc dx / dec dx
        mov [cs:m_ss], ss
        mov [cs:m_bp], bp
        mov ax, [ss:bp+0x0B]
        mov [cs:m_bps], ax
        push cs
        pop ds
        push cs
        pop es
        mov di, mline + 10
        mov ax, [m_ss]
        HEX
        add di, 4
        mov ax, [m_bp]
        HEX
        add di, 5
        mov ax, [m_bps]
        HEX
        mov si, mline
        OUTPUT
        cli
.halt:  hlt
        jmp short .halt
m_ss    dw 0
m_bp    dw 0
m_bps   dw 0
mline   db "MSLOAD ss=.... bp=.... bps=....", 13, 10, 0
        times 0x7FE - ($ - $$) db 0
        db "MS"

        section body start=0x800 vstart=0
body:   jmp short go
        times 4 - ($ - $$) db 0
        db "RHM7"                       ; 4: magic
length  dd 0                            ; 8: bytes from DATA to the end of the file
want_a  dw 0                            ; 12: expected sums over them
want_b  dw 0                            ; 14
go:     mov [cs:r_ax], ax
        mov [cs:r_bx], bx
        mov [cs:r_dx], dx
        mov [cs:r_sp], sp
        mov [cs:r_bp], bp
        mov [cs:r_ss], ss
        mov [cs:r_ds], ds
        mov [cs:r_es], es
        mov [cs:r_cs], cs
        cli
        mov ax, 0x8000
        mov ss, ax
        mov sp, 0x7000
        sti
        push cs
        pop ds
        push cs
        pop es
        ; a = sum of the bytes, b = sum of the running a: both change if any byte moves
        mov ecx, [length]
        mov si, DATA
        xor bx, bx
        xor dx, dx
        push ds
.sum:   lodsb
        xor ah, ah
        add bx, ax
        add dx, bx
        test si, si
        jnz .same
        mov ax, ds
        add ax, 0x1000
        mov ds, ax
.same:  dec ecx
        jnz .sum
        pop ds
        cmp bx, [want_a]
        jne .bad
        cmp dx, [want_b]
        jne .bad
        mov word [result], "OK"
        mov word [result + 2], 0x0A0D
.bad:   mov di, line + 9
        mov si, r_ax
        mov bp, 9
.reg:   lodsw
        HEX
        add di, 4
        dec bp
        jnz .reg
        mov si, line
        OUTPUT
        cli
.halt:  hlt
        jmp short .halt

r_ax    dw 0                            ; in the order of the report
r_bx    dw 0
r_dx    dw 0
r_sp    dw 0
r_bp    dw 0
r_ss    dw 0
r_ds    dw 0
r_es    dw 0
r_cs    dw 0
line    db "IOSYS ax=.... bx=.... dx=.... sp=.... bp=.... ss=.... ds=.... es=.... cs=.... sum="
result  db "BAD", 13, 10, 0, 0

        times 0x400 - ($ - $$) db 0
DATA    equ 0x400                       ; the appended data starts here
