args = command_line_args(count=nargs)

len=(SIZE(args, /N_ELEMENTS))
print, len
FOR I = 1, len-1 DO BEGIN

   mir_restore,string(args[I]), /nowait

   file_compile, '/reduction/czdata/final/final_images/Imaging_script/SMA_Imaging_Pipeline/idl2miriad_mosaic_2014jun04.pro'
   print, I
   it=STRCOMPRESS(STRING(I), /REMOVE_ALL)

   chans=sp[psf].iband
   uniq_chans=chans[UNIQ(chans, SORT(chans))]
   uniq_chans=uniq_chans[1:*]
   N_chans=N_ELEMENTS(uniq_chans)
   ind = WHERE(uniq_chans GT 48, count)

   IF (N_chans EQ 4) THEN BEGIN
      bands=['s1','s2','s3','s4']
      corr=['swarm']
   ENDIF ELSE BEGIN
      IF (count GT 0) THEN BEGIN
         asic_bands=['s01','s02','s03','s04','s05','s06','s07','s08','s09','s10','s11','s12','s13','s14','s15','s16','s17','s18','s19','s20','s21','s22','s23','s24','s25','s26','s27','s28','s29','s30','s31','s32','s33','s34','s35','s36','s37','s38','s39','s40','s41','s42','s43','s44','s45','s46','s47','s48']
         swarm_bands=['s49','s50','s51','s52']
         corr=['asic','swarm']
      ENDIF ELSE BEGIN
         bands=['s01','s02','s03','s04','s05','s06','s07','s08','s09','s10','s11','s12','s13','s14','s15','s16','s17','s18','s19','s20','s21','s22','s23','s24','s25','s26','s27','s28','s29','s30','s31','s32','s33','s34','s35','s36','s37','s38','s39','s40','s41','s42','s43','s44','s45','s46','s47','s48']
         corr=['asic']
      ENDELSE
   ENDELSE

   FOR j=0, N_ELEMENTS(corr)-1 DO BEGIN

      IF (N_ELEMENTS(corr) EQ 2) AND (corr[j] EQ 'asic') THEN BEGIN
         bands = ['s01','s02','s03','s04','s05','s06','s07','s08','s09','s10','s11','s12','s13','s14','s15','s16','s17','s18','s19','s20','s21','s22','s23','s24','s25','s26','s27','s28','s29','s30','s31','s32','s33','s34','s35','s36','s37','s38','s39','s40','s41','s42','s43','s44','s45','s46','s47','s48']
      ENDIF ELSE BEGIN
         IF (N_ELEMENTS(corr) EQ 2) AND (corr[j] EQ 'swarm') THEN BEGIN
            bands = ['s49','s50','s51','s52']
         ENDIF ELSE BEGIN
            bands=bands
         ENDELSE
      ENDELSE

      idl2miriad,dir=string(args[0])+'_MIR/'+string(args[0])+'.usb_'+string(corr[j])+'_'+string(it)+'.miriad',sideband='u',source=string(args[0]),band=[string(bands)],libfile='/reduction/czdata/final/final_images/Imaging_script/SMA_Imaging_Pipeline/libidl64mir.so',verbose=0

      idl2miriad,dir=string(args[0])+'_MIR/'+string(args[0])+'.lsb_'+string(corr[j])+'_'+string(it)+'.miriad',sideband='l',source=string(args[0]),band=[string(bands)],libfile='/reduction/czdata/final/final_images/Imaging_script/SMA_Imaging_Pipeline/libidl64mir.so',verbose=0

   ENDFOR

ENDFOR

end
