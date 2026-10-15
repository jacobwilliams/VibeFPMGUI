!> Prints a traceback like the one ifx (Intel's compiler) prints for a
!> runtime error, so the Output panel's links can be tried without ifx:
!> click a row's line number and file to open the file there. The rows name
!> real lines of the demo's files (in src/, the mathutils dependency, and
!> this test). The test itself passes.
program check_traceback
    implicit none
    call print_traceback()

contains

    subroutine print_traceback()
        print '(a)', 'forrtl: severe (170): Program Exception - stack overflow (an example: nothing went wrong)'
        print '(a)', 'Image              PC                Routine            Line        Source'
        print '(a)', 'check_traceback    00007FF7011B1BC7  Unknown               Unknown  Unknown'
        call row('00007FF7010C7F25', 'MEAN', 12, 'demo_stats.f90')
        call row('00007FF7010C05E2', 'VARIANCE', 17, 'demo_stats.f90')
        call row('00007FF7010ADF56', 'MATMUL_NAIVE', 28, 'demo_matrix.f90')
        call row('00007FF70109BD96', 'PARABOLA', 18, 'mathutils.f90')
        call row('00007FF70106112B', 'CHECK_TRACEBACK', 8, 'check_traceback.f90')
        print '(a)', 'check_traceback    00007FF70114D2EB  Unknown               Unknown  Unknown'
        print '(a)', 'KERNEL32.DLL       00007FFEAD58CD87  Unknown               Unknown  Unknown'
    end subroutine print_traceback

    subroutine row(pc, routine, line, source)
        !! One row, in ifx's columns (left-aligned, the line number right-aligned).
        character(len=*), intent(in) :: pc, routine, source
        integer, intent(in) :: line
        character(len=19) :: image_column, routine_column
        character(len=18) :: pc_column
        image_column = 'check_traceback'
        pc_column = pc
        routine_column = routine
        print '(a, a, a, i12, 2x, a)', image_column, pc_column, routine_column, line, source
    end subroutine row

end program check_traceback
