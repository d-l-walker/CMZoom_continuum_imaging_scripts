# CMZoom Continuum Imaging Scripts
<p align="justify">[Index](#index)</p>
These scripts constitute an automated imaging procedure for SMA data for the CMZoom survey. The SMA data must be fully calibrated prior to executing these scripts. To run the procedure, the main script (imscript.sh) must be executed on the command line, along with the sourcename of the region to be imaged and the paths to all SMA data files corresponding to said region, i.e.: 

> ./imscript.sh G0.380+0.050 /reduction/czdata/final/final_cal/track2_final_DW.mir /reduction/czdata/final/final_cal/track21_final_if1_xl.mir /reduction/czdata/final/final_cal/track21_final_if2_xl.mir

