import sys,io,img2pdf,pikepdf,glob,os
A4=(img2pdf.mm_to_pt(210),img2pdf.mm_to_pt(297))
M=img2pdf.mm_to_pt(15)
def lay(wpx,hpx,ndpi):
    w=wpx*72.0/ndpi[0]; h=hpx*72.0/ndpi[1]
    k=min(1.0,(A4[0]-2*M)/w,(A4[1]-2*M)/h)
    return A4[0],A4[1],w*k,h*k
for g in range(1,6):
    mail=f'in/g{g}_mail.pdf'
    atts=sorted(glob.glob(f'in/g{g}_a*'))
    out=pikepdf.open(mail)
    for a in atts:
        if a.endswith('.pdf'):
            src=pikepdf.open(a); out.pages.extend(src.pages)
        else:
            data=img2pdf.convert(a,layout_fun=lay,rotation=img2pdf.Rotation.ifvalid)
            src=pikepdf.open(io.BytesIO(data)); out.pages.extend(src.pages)
    out.save(f'out/g{g}.pdf'); print(g,len(out.pages))
