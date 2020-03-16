# CMZoom Continuum Imaging Scripts

These scripts constitute an automated imaging procedure for SMA data for the CMZoom survey. The SMA data must be fully calibrated prior to executing these scripts. To run the procedure, the main script (imscript.sh) must be executed on the command line, along with the sourcename of the region to be imaged and the paths to all SMA data files corresponding to said region, i.e.: 

> ./imscript.sh G0.380+0.050 /reduction/czdata/final/final_cal/track2_final_DW.mir /reduction/czdata/final/final_cal/track21_final_if1_xl.mir /reduction/czdata/final/final_cal/track21_final_if2_xl.mir

This script broadly does the following:

- Creates directory for given sourcename
- Runs an IDL script (mir_output_to_miriad.pro) to output sideband data in .miriad format
- For each .miriad file, edge channels are flagged and data are output as .UVFITS
- Sourcename is converted into J2000 format to be used later in CASA
- Run_find_cont.py is executed in CASA to determine continuum channels for each file
- Run_tclean.py is executed in CASA to generate continuum mosaic from all data associated with the given region
